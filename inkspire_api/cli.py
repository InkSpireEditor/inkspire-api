# -*- coding: utf-8 -*-
"""Administration from a shell.

Accounts, because the API exposes no registration and no password change: these two
commands are the only way to create an account or set its password, and both act
without knowing the current password, which is why they have no HTTP equivalent.

    inkspire user list
    inkspire user create alice@example.com
    inkspire user create alice@example.com --password '...' --role ROLE_ADMIN
    inkspire user reset-password alice@example.com
    inkspire user reset-password alice@example.com --keep-sessions

Generation, which runs the same prompt and the same providers the API serves, with
no account and no HTTP in the way:

    inkspire llm models
    inkspire llm generate -m local-ollama/llama3 < chapter.ink
    inkspire llm title < chapter.ink
    inkspire llm summary < chapter.ink

The `.ink` files themselves, since the API is forgiving about a header it cannot read
and will show such a file under its filename rather than hide it:

    inkspire ink check
    inkspire ink check stories/example-story

And the server itself:

    inkspire run
    inkspire run --reload --port 8001
"""

from __future__ import annotations

import asyncio
import re
import secrets
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, cast

import typer
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.exc import IntegrityError

from . import context_summary, ink, prompt, provenance, repository, summaries, titles
from .db import get_sessionmaker
from .fs import write_atomically
from .llm import LLMError, LLMService, UnknownModel
from .models import MAX_EMAIL_LENGTH, RefreshToken, User, utcnow
from .security import hash_password
from .settings import SecretNotConfigured, Settings, get_settings
from .storage import CHAPTER_SUFFIX

#: Bytes of randomness behind a generated password. Hex-encoded, so 24 characters.
GENERATED_PASSWORD_BYTES = 12

MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 4096

#: A local part, an @, and a domain containing a dot. This catches a mistyped
#: address; it is not an RFC 5322 check, and only delivery proves an address real.
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = typer.Typer(help="InkSpire administration.", no_args_is_help=True)
user_app = typer.Typer(help="Account administration.", no_args_is_help=True)
llm_app = typer.Typer(help="Text generation.", no_args_is_help=True)
ink_app = typer.Typer(help="The .ink files themselves.", no_args_is_help=True)
app.add_typer(user_app, name="user")
app.add_typer(llm_app, name="llm")
app.add_typer(ink_app, name="ink")

EmailArgument = Annotated[str, typer.Argument(help="Email address of the account")]
PasswordOption = Annotated[
    str | None,
    typer.Option(
        "--password", "-p", help="Omit to generate a random one and print it."
    ),
]


def fail(message: str) -> None:
    """Prints an error to stderr and exits non-zero."""
    typer.secho(f"[ERROR] {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def generate_password() -> str:
    """A random password, shown once and stored only as a hash."""
    return secrets.token_hex(GENERATED_PASSWORD_BYTES)


def check_password(password: str) -> None:
    """Rejects a password outside the accepted length range.

    The upper bound is a policy limit. Only the first 72 bytes of a password reach
    bcrypt, whatever length is allowed here.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        fail(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        fail(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")


def normalise_roles(roles: list[str]) -> list[str]:
    """Upper-cases roles, refuses one not starting with ROLE_, and drops duplicates."""
    checked = []
    for role in roles:
        role = role.strip().upper()
        if not role.startswith("ROLE_"):
            fail(f'Invalid role "{role}": roles must start with ROLE_.')
        checked.append(role)
    return list(dict.fromkeys(checked))


@app.command("run")
def run(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart when a source file changes.")
    ] = False,
) -> None:
    """Serve the API.

    One process only. The model cache and both rate limiters live in the serving
    process, so running several would multiply the effective generation limit by the
    number of them.
    """
    # The signing secret is what the server checks on the way up. Checking it first
    # turns a traceback from inside startup into one line.
    settings = get_settings()
    try:
        settings.jwt_secret_or_raise()
    except SecretNotConfigured as error:
        fail(str(error))

    if not settings.data_root.is_dir():
        typer.secho(
            f"No stories ({settings.data_root} is not there), so the tree will be "
            "empty. Clone the story repository, or set INKSPIRE_DATA_ROOT.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if not settings.llm_providers_file.is_file():
        typer.secho(
            f"No providers configured ({settings.llm_providers_file} is absent), so no "
            "model will be offered. See the README.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    # Imported here so the account commands do not pay for the server's import.
    import uvicorn  # pylint: disable=import-outside-toplevel

    uvicorn.run("inkspire_api.main:app", host=host, port=port, reload=reload)


@user_app.command("list")
def list_users() -> None:
    """List the accounts, with their roles and how many sessions each has open.

    A session is a refresh token that has not expired. Expired ones stay in the table
    until the client next tries to use them, and are not counted here.
    """
    with get_sessionmaker()() as session:
        accounts = session.scalars(select(User).order_by(User.email)).all()
        if not accounts:
            typer.secho(
                "No accounts. Create one with `inkspire user create`.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            return

        live = dict(
            session.execute(
                select(RefreshToken.user_id, func.count())  # pylint: disable=not-callable
                .where(RefreshToken.expires_at > utcnow())
                .group_by(RefreshToken.user_id)
            )
            .tuples()
            .all()
        )

        # Padded to the longest address so the columns line up, and separated by two
        # spaces so a reader can still cut on whitespace.
        width = max(len(account.email) for account in accounts)
        typer.secho(
            f"{'EMAIL':<{width}}  {'ROLES':<32}  SESSIONS", fg=typer.colors.BLUE, err=True
        )
        for account in accounts:
            roles = ",".join(account.all_roles())
            typer.echo(f"{account.email:<{width}}  {roles:<32}  {live.get(account.id, 0)}")


@user_app.command("create")
def create(
    email: EmailArgument,
    password: PasswordOption = None,
    role: Annotated[
        list[str] | None,
        typer.Option("--role", "-r", help="Grant a role (repeatable). ROLE_USER is always implied."),
    ] = None,
) -> None:
    """Create an account."""
    settings = get_settings()
    email = email.strip()

    if not EMAIL.match(email):
        fail(f'"{email}" is not a valid email address.')
    if len(email) > MAX_EMAIL_LENGTH:
        fail(f"Email must be at most {MAX_EMAIL_LENGTH} characters.")

    roles = normalise_roles(role or [])

    generated = password is None
    if password is None:
        password = generate_password()
    check_password(password)

    with get_sessionmaker()() as session:
        if session.scalar(select(User).where(User.email == email)) is not None:
            fail(f'An account already exists for "{email}".')

        account = User(
            email=email,
            roles=roles,
            password=hash_password(password, settings.bcrypt_rounds),
        )
        session.add(account)
        try:
            session.commit()
        except IntegrityError:
            # The lookup above races with a concurrent run; the unique index on
            # email is what actually guarantees it.
            session.rollback()
            fail(f'An account already exists for "{email}".')

        typer.secho(f"[OK] Created {email}.", fg=typer.colors.GREEN)
        if generated:
            # The only time this password is visible. It is stored only as a hash.
            typer.echo(f"  Generated password: {password}")
        typer.echo(f"  Roles: {', '.join(account.all_roles())}")


@user_app.command("reset-password")
def reset_password(
    email: EmailArgument,
    password: PasswordOption = None,
    keep_sessions: Annotated[
        bool,
        typer.Option(
            "--keep-sessions",
            help="Leave existing refresh tokens valid instead of revoking them.",
        ),
    ] = False,
) -> None:
    """Set an account's password and revoke its sessions."""
    settings = get_settings()

    generated = password is None
    if password is None:
        password = generate_password()
    check_password(password)

    with get_sessionmaker()() as session:
        account = session.scalar(select(User).where(User.email == email))
        if account is None:
            fail(f'No account found for "{email}".')
            return  # unreachable; fail() exits, and this tells the type checker so

        account.password = hash_password(password, settings.bcrypt_rounds)

        # A reset that leaves refresh tokens alive is incomplete: a stolen token
        # would keep minting JWTs for the rest of its lifetime.
        revoked = 0
        if not keep_sessions:
            # A DELETE answers with a cursor, which is what carries the row count.
            deleted = cast(
                CursorResult,
                session.execute(
                    delete(RefreshToken).where(RefreshToken.user_id == account.id)
                ),
            )
            revoked = deleted.rowcount
        session.commit()

        typer.secho(f"[OK] Password updated for {email}.", fg=typer.colors.GREEN)
        if generated:
            typer.echo(f"  Generated password: {password}")
        if keep_sessions:
            typer.secho(
                "[WARNING] Existing sessions were left active (--keep-sessions).",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo(f"  Refresh tokens revoked: {revoked}")


# --- llm -------------------------------------------------------------------


def service(think: bool | None = None, send_selection: bool | None = None) -> LLMService:
    """The same service the API uses, reading the same provider file.

    The default path is relative, so run these commands from the project directory or
    set INKSPIRE_LLM_PROVIDERS_FILE.
    """
    settings = get_settings()
    overrides: dict[str, bool] = {}
    if think is not None:
        overrides["llm_think"] = think
    if send_selection is not None:
        overrides["llm_send_selection"] = send_selection
    if overrides:
        settings = settings.model_copy(update=overrides)
    try:
        return LLMService(settings)
    except ValueError as error:  # an unreadable or misshapen provider file
        fail(str(error))
        raise  # unreachable: fail() exits


@llm_app.command("models")
def list_models() -> None:
    """List the models every configured provider offers."""
    models = asyncio.run(service().models())
    if not models:
        typer.secho(
            "No models. Check that a provider is configured and reachable.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)
    for model in models:
        typer.echo(model["name"])


@llm_app.command("generate")
def generate(
    model: Annotated[
        str, typer.Option("--model", "-m", help="Model to generate with, as listed by `llm models`.")
    ],
    *,
    think: Annotated[
        bool | None,
        typer.Option(
            "--think/--no-think",
            help=(
                "Ask a reasoning model to think, or not to. Left unset the key is not "
                "sent at all. Thinking delays the first chunk by the whole reasoning pass."
            ),
        ),
    ] = None,
    show_prompt: Annotated[
        bool,
        typer.Option("--show-prompt", help="Print the rendered prompt and generate nothing."),
    ] = False,
    cursor: Annotated[
        int | None,
        typer.Option(
            "--cursor",
            help=(
                "A flat character offset into standard input, marking where the "
                "caret is -- generates a fill-in-the-middle prompt instead of a "
                "continuation. Left unset, generation continues at the end. Not "
                "combinable with --select."
            ),
        ),
    ] = None,
    select_range: Annotated[
        str | None,
        typer.Option(
            "--select",
            help=(
                "Two flat character offsets into standard input, as START:END, "
                "marking a passage to rewrite instead of continuing or filling in. "
                "Not combinable with --cursor."
            ),
        ),
    ] = None,
    send_selection: Annotated[
        bool | None,
        typer.Option(
            "--send-selection/--no-send-selection",
            help=(
                "For --select, send the passage's own text along with its word "
                "count, rather than the word count alone. Left unset, the server's "
                "own setting (INKSPIRE_LLM_SEND_SELECTION) applies. Has no effect "
                "without --select."
            ),
        ),
    ] = None,
    synopsis: Annotated[
        str,
        typer.Option(
            "--synopsis",
            help=(
                "The containing story's synopsis or notes folder's context, as the "
                "API would read it from story.yaml or a folder's manifest (api#17). "
                "Left unset, the prompt makes no mention of one."
            ),
        ),
    ] = "",
) -> None:
    """Continue the text read from standard input, printing chunks as they arrive.

        inkspire llm generate -m local-ollama/llama3 < chapter.ink

    The text is wrapped in the same prompt the API sends, so what a model does here is
    what it does for a writer in the editor. Output is the continuation alone, which
    can be redirected; progress and timings go to standard error.
    """
    if cursor is not None and select_range is not None:
        fail("--cursor and --select cannot both be given.")

    text = sys.stdin.read()
    if not text.strip():
        fail("No text on standard input. Pipe a file or type text and end with Ctrl-D.")

    at_cursor = prompt.cursor_from_offset(text, cursor) if cursor is not None else None
    at_selection = _parse_select(select_range, text) if select_range is not None else None

    if show_prompt:
        svc = service(think, send_selection)
        try:
            rendered = prompt.render(
                prompt.assemble(
                    text,
                    budget=svc.defaults.prompt_budget,
                    cursor=at_cursor,
                    selection=at_selection,
                    prefix_share=svc.defaults.prefix_share,
                    send_selection=svc.defaults.send_selection,
                    synopsis=synopsis,
                )
            )
        except prompt.CursorOutOfRange as error:
            fail(str(error))
        else:
            typer.echo(rendered)
        return

    asyncio.run(
        _stream(
            model,
            text,
            think,
            cursor=at_cursor,
            selection=at_selection,
            send_selection=send_selection,
            synopsis=synopsis,
        )
    )


@llm_app.command("title")
def title_command(
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help=(
                "Model to ask, as listed by `llm models`. Left unset, "
                "INKSPIRE_LLM_SMALL_MODEL applies -- the same model the API's "
                "title route uses."
            ),
        ),
    ] = None,
    *,
    current_title: Annotated[
        str,
        typer.Option(
            "--current-title",
            "-t",
            help=(
                "The chapter's title already, as the dice button would send. Left "
                "unset, the prompt makes no mention of one -- the same as a chapter "
                "with no title at all."
            ),
        ),
    ] = "",
    instruction: Annotated[
        str,
        typer.Option(
            "--instruction",
            "-i",
            help=(
                "A short steering note, as the title-edit modal's own field would "
                "send -- \"make it ominous\", say. Left unset, the prompt makes no "
                "mention of one, the same as the dice button's own plain roll."
            ),
        ),
    ] = "",
    show_prompt: Annotated[
        bool,
        typer.Option("--show-prompt", help="Print the rendered prompt and ask nothing."),
    ] = False,
) -> None:
    """Propose a title for the chapter read from standard input.

        inkspire llm title < chapter.ink
        inkspire llm title --current-title "The Wax Still Held" --show-prompt < chapter.ink
        inkspire llm title --instruction "make it ominous" --show-prompt < chapter.ink

    Wrapped in the same prompt the API's title route sends (`titles.py`), so what a
    model answers here is what a writer would be offered through the dice button.
    Always asks with thinking disabled, as the route does -- there is no --think
    here. Output is the cleaned title alone; progress goes nowhere, since this is
    one short answer rather than a stream.
    """
    text = sys.stdin.read()
    if not text.strip():
        fail("No text on standard input. Pipe a file or type text and end with Ctrl-D.")

    svc = service(think=False)
    context = titles.assemble_title(
        text,
        budget=svc.defaults.prompt_budget,
        current_title=current_title,
        instruction=instruction,
    )
    rendered = titles.render_title(context)

    if show_prompt:
        typer.echo(rendered)
        return

    chosen_model = model or get_settings().llm_small_model
    if chosen_model is None:
        fail("No model given and INKSPIRE_LLM_SMALL_MODEL is unset.")
        raise AssertionError  # unreachable: fail() exits

    try:
        raw = asyncio.run(svc.complete(chosen_model, rendered))
    except (UnknownModel, LLMError) as error:
        fail(str(error))
        raise AssertionError from error  # unreachable: fail() exits

    cleaned = titles.clean_title(raw)
    if not cleaned:
        fail("The model answered with nothing usable as a title.")
    typer.echo(cleaned)


@llm_app.command("summary")
def summary_command(
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help=(
                "Model to ask, as listed by `llm models`. Left unset, "
                "INKSPIRE_LLM_SMALL_MODEL applies -- the same model the background "
                "context-summary call uses (api#18)."
            ),
        ),
    ] = None,
    *,
    budget: Annotated[
        int,
        typer.Option(
            "--budget",
            help=(
                "The prompt budget to trim standard input against, as "
                "INKSPIRE_LLM_PROMPT_BUDGET does for a real generation -- what this "
                "command summarises is whatever a continuation at this budget would "
                "drop, not the whole of standard input."
            ),
        ),
    ] = 10_000,
    show_prompt: Annotated[
        bool,
        typer.Option("--show-prompt", help="Print the rendered prompt and ask nothing."),
    ] = False,
) -> None:
    """Summarise what a continuation at `--budget` characters would drop from the
    chapter read from standard input.

        inkspire llm summary < chapter.ink
        inkspire llm summary --budget 2000 --show-prompt < chapter.ink

    The only way to iterate on `templates/summary.j2` against a real model without
    saving a file large enough to trigger the background call's own trigger metric
    (`context_summary.is_due`). Prints the dropped character count to standard error
    alongside the answer -- the same number `is_due` compares a later trim's own
    count against. Always asks with thinking disabled, as the background call does.
    """
    text = sys.stdin.read()
    if not text.strip():
        fail("No text on standard input. Pipe a file or type text and end with Ctrl-D.")

    dropped_text = context_summary.dropped(text, budget)
    if not dropped_text:
        fail(f"Nothing would be dropped: the text is within the {budget}-character budget.")

    context = summaries.assemble_summary(dropped_text, budget=budget)
    rendered = summaries.render_summary(context)

    if show_prompt:
        typer.echo(rendered)
        return

    chosen_model = model or get_settings().llm_small_model
    if chosen_model is None:
        fail("No model given and INKSPIRE_LLM_SMALL_MODEL is unset.")
        raise AssertionError  # unreachable: fail() exits

    svc = service(think=False)
    try:
        raw = asyncio.run(svc.complete(chosen_model, rendered))
    except (UnknownModel, LLMError) as error:
        fail(str(error))
        raise AssertionError from error  # unreachable: fail() exits

    cleaned = summaries.clean_summary(raw)
    if not cleaned:
        fail("The model answered with nothing usable as a summary.")
    typer.secho(f"{len(dropped_text)} characters would be dropped.", fg=typer.colors.BLUE, err=True)
    typer.echo(cleaned)


def _parse_select(select_range: str, text: str) -> prompt.CursorRange:
    """`--select`'s `START:END` into a `CursorRange`, each offset resolved the same
    way `--cursor` resolves one."""
    raw_start, separator, raw_end = select_range.partition(":")
    if not separator:
        fail('--select must be two offsets separated by a colon, as in "500:720".')
        raise AssertionError  # unreachable: fail() exits
    try:
        start, end = int(raw_start), int(raw_end)
    except ValueError as error:
        fail(f'--select\'s offsets must be integers; got "{select_range}".')
        raise AssertionError from error  # unreachable: fail() exits
    return prompt.CursorRange(
        start=prompt.cursor_from_offset(text, start),
        end=prompt.cursor_from_offset(text, end),
    )


async def _stream(
    model: str,
    text: str,
    think: bool | None,
    *,
    cursor: prompt.Cursor | None = None,
    selection: prompt.CursorRange | None = None,
    send_selection: bool | None = None,
    synopsis: str = "",
) -> None:
    started = time.monotonic()
    first_chunk_at: float | None = None
    characters = 0

    try:
        async for chunk in service(think, send_selection).stream(
            model, text, cursor=cursor, selection=selection, synopsis=synopsis
        ):
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
            characters += len(chunk)
            # Written and flushed per chunk: buffering here would defeat the point of
            # streaming, since nothing would appear until the generation finished.
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except (UnknownModel, LLMError, prompt.CursorOutOfRange) as error:
        sys.stdout.flush()
        fail(str(error))

    sys.stdout.write("\n")
    sys.stdout.flush()

    if first_chunk_at is None:
        typer.secho("The provider sent no text.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"{characters} characters, first after {first_chunk_at - started:.1f}s, "
        f"{time.monotonic() - started:.1f}s total",
        fg=typer.colors.BLUE,
        err=True,
    )


# --- the .ink files ---------------------------------------------------------


def plural(number: int, thing: str) -> str:
    """`1 file`, `2 files`."""
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


def shown(path: Path) -> str:
    """The path as a reader can retype it: relative to where they are, if it is."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def ink_files(targets: list[Path]) -> list[Path]:
    """Every `.ink` file in `targets`, which may name files or directories."""
    found: list[Path] = []
    for target in targets:
        if target.is_dir():
            found.extend(
                path
                for path in target.rglob(f"*{CHAPTER_SUFFIX}")
                if path.is_file() and not path.is_symlink()
            )
        elif target.exists():
            found.append(target)
        else:
            fail(f"{target} is not there.")
    return sorted(set(found))


def problems_in(path: Path) -> list[ink.Problem]:
    """What is wrong with one file, a file that cannot be read at all included."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [ink.Problem(1, ink.ERROR, f"it cannot be read as text: {error}")]
    return ink.check(text)


@ink_app.command("check")
def check(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Files or directories to check. Omit for both roots."),
    ] = None,
) -> None:
    """Report what is wrong with the header of each `.ink` file.

    An error is something the application cannot read, and a file carrying one is
    shown under its filename with its header left as prose. A warning is something it
    reads and does not understand, such as a key it has no meaning for. Exits non-zero
    if there was an error, so this can gate a commit.

    With no argument this covers both roots: the stories, and the files that are not a
    novel. A root that is not there holds no files and is not an error.
    """
    settings = get_settings()
    # A root nobody has written to yet simply holds no files. A path named on the
    # command line and not there is a mistake worth reporting.
    roots = [
        root
        for root in (settings.data_root, settings.files_root)
        if root.is_dir()
    ]
    files = ink_files(list(paths) if paths else roots)
    if not files:
        typer.secho("No .ink files to check.", fg=typer.colors.YELLOW, err=True)
        return

    errors = 0
    warnings = 0
    for path in files:
        for problem in problems_in(path):
            colour = typer.colors.RED if problem.level == ink.ERROR else typer.colors.YELLOW
            typer.secho(
                f"{shown(path)}:{problem.line}: {problem.level}: {problem.message}",
                fg=colour,
            )
            if problem.level == ink.ERROR:
                errors += 1
            else:
                warnings += 1

    summary = (
        f"{plural(len(files), 'file')} checked, "
        f"{plural(errors, 'error')}, {plural(warnings, 'warning')}."
    )
    if errors:
        typer.secho(summary, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(
        summary, fg=typer.colors.YELLOW if warnings else typer.colors.GREEN, err=True
    )


# --- reclassifying provenance -----------------------------------------------

#: How a hash is shown: enough to tell two apart, short enough to read in a column.
SHOWN_HASH = 4


@dataclass(frozen=True)
class Verdict:
    """What `reclassify` found about one paragraph."""

    #: Its place in the body, counting from 1, which is how a reader finds it.
    index: int
    #: The hash the paragraph has now.
    digest: str
    #: `ok`, `recovered` or `reset`.
    state: str
    #: The stale key this was recovered from, where it was.
    was: str | None = None
    #: How many runs came through.
    runs: int = 0


def brief(digest: str) -> str:
    """`47f5…330e`: both ends of a hash, which is what a reader compares."""
    return f"{digest[:SHOWN_HASH]}…{digest[-SHOWN_HASH:]}"


def repository_for(path: Path, settings: Settings) -> Path | None:
    """The git working tree `path` sits in, or `None` where it sits in none.

    Only the stories root is a repository. The notes root is not one, and a stories
    root nobody has cloned is not either — in both cases a stale paragraph resets,
    which is what `repository.reconciled` does with no repository (§7.5).
    """
    try:
        path.resolve().relative_to(settings.data_root.resolve())
    except ValueError:
        return None
    return settings.data_root if (settings.data_root / ".git").exists() else None


def verdicts_for(
    body: str, stored: provenance.Metadata, recovery: repository.Recovery
) -> list[Verdict]:
    """One verdict per paragraph of `body`.

    A paragraph whose hash was already stored was never stale. One whose hash was not
    is stale, and `recovery.sources` says which stored key explained it — that pairing
    is decided by similarity (§7.5), so it has to be reported by the recovery rather
    than reconstructed here from the key sets, which could only guess.
    """
    found: list[Verdict] = []
    for index, digest in enumerate(provenance.hashes_of(body), start=1):
        if digest in stored:
            found.append(Verdict(index, digest, "ok"))
        elif digest in recovery.sources:
            found.append(
                Verdict(
                    index,
                    digest,
                    "recovered",
                    was=recovery.sources[digest],
                    runs=len(recovery.metadata[digest]),
                )
            )
        else:
            found.append(Verdict(index, digest, "reset"))
    return found


def reclassify_file(path: Path, settings: Settings) -> tuple[list[Verdict], str | None, str | None]:
    """What one file needs, as verdicts, the revision used, and the text to write.

    The text is `None` where there is nothing to write — the file has no provenance
    section, or every paragraph's hash already matches. A file whose section cannot be
    read at all answers no verdicts, so the caller reports it and leaves it alone:
    there is no recovery to apply to a section nobody can parse.
    """
    document = ink.parse(path.read_text(encoding="utf-8"))
    section = document.sections.get(ink.SECTION_PROVENANCE)
    if section is None:
        return [], None, None

    stored = provenance.parse_section(section)
    if stored is None:
        return [], None, None

    root = repository_for(path, settings)
    if root is None:
        recovery = repository.reconciled(
            None, PurePosixPath(path.name), document.body, stored
        )
    else:
        relpath = PurePosixPath(path.resolve().relative_to(root.resolve()))
        with repository.open_repository(root) as repo:
            # No ceiling here, unlike a request: this is run by hand, after a miss, and
            # is worth however long the whole history takes.
            recovery = repository.reconciled(
                repo, relpath, document.body, stored, limit=None
            )

    found = verdicts_for(document.body, stored, recovery)
    if set(recovery.metadata) == set(stored):
        return found, None, None
    return (
        found,
        recovery.revision,
        ink.render_with(
            document,
            document.body,
            ink.SECTION_PROVENANCE,
            provenance.render_section(recovery.metadata),
        ),
    )


def report(verdict: Verdict, revision: str | None) -> None:
    """One paragraph's line of the report."""
    if verdict.state == "ok":
        typer.secho(f"  para {verdict.index}  {brief(verdict.digest)}  ok")
        return
    if verdict.state == "recovered":
        was = f", was {brief(verdict.was)}" if verdict.was else ""
        typer.secho(
            f"  para {verdict.index}  {brief(verdict.digest)}  STALE  -> recoverable"
            f" from {revision}{was} ({plural(verdict.runs, 'run')})",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(
        f"  para {verdict.index}  {brief(verdict.digest)}  STALE  -> would reset",
        fg=typer.colors.RED,
    )


def reclassify_one(path: Path, settings: Settings, *, force: bool) -> Counter[str]:
    """Reports one file, writes it where `force` says to, and counts what it found."""
    counts: Counter[str] = Counter()
    try:
        found, revision, text = reclassify_file(path, settings)
    except (OSError, UnicodeDecodeError) as error:
        typer.secho(f"{shown(path)}: cannot be read as text: {error}", fg=typer.colors.RED)
        counts["stale"] += 1
        return counts

    if text is None:
        return counts

    typer.secho(shown(path))
    for verdict in found:
        report(verdict, revision)
        if verdict.state == "recovered":
            counts["stale"] += 1
            counts["recoverable"] += 1
        elif verdict.state == "reset":
            counts["stale"] += 1
            counts["would_reset"] += 1

    if force:
        write_atomically(path, text)
        counts["written"] += 1
    return counts


@ink_app.command("reclassify")
def reclassify(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Files or directories to reclassify. Omit for both roots."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Write the recovered provenance.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Report and write nothing. The default.")
    ] = False,
) -> None:
    """Put right the provenance of a paragraph edited outside the editor.

    A paragraph edited by hand no longer hashes to the key its runs are stored under,
    so those runs describe prose that is no longer there. Recomputing the hash is not
    the fix: it would record the new text while keeping offsets measured against the
    old. Instead the paragraph's own earlier text is looked for in the file's history,
    diffed forward, and the runs replayed over the result. What history cannot explain
    resets to plain prose rather than being guessed at.

    **Nothing is written without `--force`.** A dry run exits non-zero if it found
    anything stale, so it can gate a commit; `--force` exits zero once it has resolved
    it. A notes file has no history and can only ever reset.

    With no argument this covers both roots, as `ink check` does.
    """
    if force and dry_run:
        fail("Pass --force or --dry-run, not both.")

    settings = get_settings()
    roots = [root for root in (settings.data_root, settings.files_root) if root.is_dir()]
    files = ink_files(list(paths) if paths else roots)
    if not files:
        typer.secho("No .ink files to reclassify.", fg=typer.colors.YELLOW, err=True)
        return

    counts: Counter[str] = Counter()
    for path in files:
        counts += reclassify_one(path, settings, force=force)

    summary = (
        f"{plural(len(files), 'file')} checked, {counts['stale']} stale, "
        f"{counts['recoverable']} recoverable, {counts['would_reset']} would reset."
    )
    if force:
        typer.secho(
            f"{summary} {plural(counts['written'], 'file')} written.",
            fg=typer.colors.GREEN,
            err=True,
        )
        return
    if counts["stale"]:
        typer.secho(
            f"{summary} Nothing written; pass --force to apply.", fg=typer.colors.YELLOW, err=True
        )
        raise typer.Exit(code=1)
    typer.secho(summary, fg=typer.colors.GREEN, err=True)
