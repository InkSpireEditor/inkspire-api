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
from pathlib import Path
from typing import Annotated, cast

import typer
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.exc import IntegrityError

from . import ink
from .db import get_sessionmaker
from .llm import LLMError, LLMService, UnknownModel, render_prompt
from .models import MAX_EMAIL_LENGTH, RefreshToken, User, utcnow
from .security import hash_password
from .settings import SecretNotConfigured, get_settings
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


def service(think: bool | None = None) -> LLMService:
    """The same service the API uses, reading the same provider file.

    The default path is relative, so run these commands from the project directory or
    set INKSPIRE_LLM_PROVIDERS_FILE.
    """
    settings = get_settings()
    if think is not None:
        settings = settings.model_copy(update={"llm_think": think})
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
) -> None:
    """Continue the text read from standard input, printing chunks as they arrive.

        inkspire llm generate -m local-ollama/llama3 < chapter.ink

    The text is wrapped in the same prompt the API sends, so what a model does here is
    what it does for a writer in the editor. Output is the continuation alone, which
    can be redirected; progress and timings go to standard error.
    """
    text = sys.stdin.read()
    if not text.strip():
        fail("No text on standard input. Pipe a file or type text and end with Ctrl-D.")

    if show_prompt:
        typer.echo(render_prompt(text))
        return

    asyncio.run(_stream(model, text, think))


async def _stream(model: str, text: str, think: bool | None) -> None:
    started = time.monotonic()
    first_chunk_at: float | None = None
    characters = 0

    try:
        async for chunk in service(think).stream(model, text):
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
            characters += len(chunk)
            # Written and flushed per chunk: buffering here would defeat the point of
            # streaming, since nothing would appear until the generation finished.
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except (UnknownModel, LLMError) as error:
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
