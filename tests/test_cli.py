# -*- coding: utf-8 -*-
"""The commands: `inkspire user …`, `inkspire llm …` and `inkspire ink check`.

Several account tests end at the login endpoint rather than at the hasher: what
matters about those commands is that the account they write can actually be used, and
that a reset actually invalidates what came before.

The generation tests check what reaches the provider and where output goes, since the
command exists to exercise the same prompt and providers the API serves.

The checker is tested through its exit code as much as its output: it is meant to gate
a commit, so an error has to be the difference between 0 and 1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from git import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from tests.conftest import (
    EMAIL,
    PASSWORD,
    add_refresh_token,
    answer_body,
    delta,
    llm_settings,
    sse_body,
)

from inkspire_api import cli, provenance
from inkspire_api import prompt as prompt_lib
from inkspire_api.llm import LLMService
from inkspire_api.models import RefreshToken, User
from inkspire_api.security import verify_password
from inkspire_api.settings import Settings

NEW_EMAIL = "new-user@example.com"
GOOD_PASSWORD = "a-good-password"


def render_prompt(
    text: str,
    budget: int = Settings().llm_prompt_budget,
    *,
    cursor: prompt_lib.Cursor | None = None,
    selection: prompt_lib.CursorRange | None = None,
    send_selection: bool = Settings().llm_send_selection,
) -> str:
    """What `generate --show-prompt` and a real generation both send, now that
    assembly is `prompt.py`'s two calls rather than one bare function.

    `send_selection` defaults to the server's own setting, matching `service()`'s own
    behaviour when `generate` is given no `--send-selection`/`--no-send-selection`.
    """
    prefix_share = Settings().llm_prefix_share
    return prompt_lib.render(
        prompt_lib.assemble(
            text,
            budget=budget,
            cursor=cursor,
            selection=selection,
            prefix_share=prefix_share,
            send_selection=send_selection,
        )
    )

runner = CliRunner()


@pytest.fixture
def invoke(
    monkeypatch, settings: Settings, session_factory: sessionmaker[Session]
):
    """Runs a CLI command against the test database.

    The command module reaches for the process-wide engine and settings, so both
    are redirected here rather than through the environment, which would need the
    lru_caches cleared.
    """
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: session_factory)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def run(*args: str):
        return runner.invoke(cli.app, ["user", *args])

    return run


def find(session: Session, email: str) -> User | None:
    session.expire_all()
    return session.scalar(select(User).where(User.email == email))


# --- run -------------------------------------------------------------------


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """Runs `inkspire run` with the server itself stubbed out, returning its arguments."""
    started: dict = {}

    def record(target, **kwargs):
        started["target"] = target
        started.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", record)

    def run(
        *args: str,
        secret: str | None = "s" * 32,
        providers: bool = True,
        stories: bool = True,
    ):
        settings = llm_settings(tmp_path) if providers else Settings(
            llm_providers_file=tmp_path / "absent.yaml"
        )
        data_root = tmp_path / "novel-data"
        if stories:
            (data_root / "stories").mkdir(parents=True, exist_ok=True)
        settings = settings.model_copy(
            update={"jwt_secret": secret, "data_root": data_root}
        )
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        return runner.invoke(cli.app, ["run", *args]), started

    return run


def test_run_serves_on_localhost_by_default(serve) -> None:
    result, started = serve()
    assert result.exit_code == 0, result.output
    assert started["target"] == "inkspire_api.main:app"
    assert started["host"] == "127.0.0.1"
    assert started["port"] == 8000
    assert started["reload"] is False


def test_run_takes_a_host_and_port(serve) -> None:
    result, started = serve("--host", "0.0.0.0", "--port", "9000")
    assert result.exit_code == 0, result.output
    assert (started["host"], started["port"]) == ("0.0.0.0", 9000)


def test_run_can_reload(serve) -> None:
    _, started = serve("--reload")
    assert started["reload"] is True


def test_run_without_a_secret_fails_before_starting(serve) -> None:
    """One line beats a traceback out of the middle of startup."""
    result, started = serve(secret=None)
    assert result.exit_code == 1
    assert "INKSPIRE_JWT_SECRET" in result.output
    assert started == {}, "the server must not have been started"


def test_run_warns_when_no_provider_is_configured(serve) -> None:
    """The API still serves; it just has no model to offer, which is worth saying."""
    result, started = serve(providers=False)
    assert result.exit_code == 0, result.output
    assert "No providers configured" in result.output
    assert started["target"] == "inkspire_api.main:app"


def test_run_warns_when_the_stories_are_not_there(serve) -> None:
    """An empty tree is what an unconfigured story repository looks like from a browser."""
    result, started = serve(stories=False)
    assert result.exit_code == 0, result.output
    assert "INKSPIRE_DATA_ROOT" in result.output
    assert started["target"] == "inkspire_api.main:app"


# --- user list -------------------------------------------------------------


def test_listing_no_accounts_says_so(invoke) -> None:
    result = invoke("list")
    assert result.exit_code == 0
    assert "No accounts" in result.output


def test_accounts_are_listed_with_roles_and_session_counts(
    invoke, user: User, session_factory: sessionmaker[Session]
) -> None:
    add_refresh_token(session_factory, user, expires_in=3600)
    assert invoke("create", "admin@example.com", "-p", GOOD_PASSWORD, "-r", "ROLE_ADMIN").exit_code == 0

    result = invoke("list")
    assert result.exit_code == 0, result.output

    rows = {line.split()[0]: line.split() for line in result.stdout.splitlines() if line.strip()}
    assert rows[EMAIL][1] == "ROLE_USER"
    assert rows[EMAIL][2] == "1"
    assert rows["admin@example.com"][1] == "ROLE_ADMIN,ROLE_USER"
    assert rows["admin@example.com"][2] == "0"


def test_accounts_are_listed_in_email_order(invoke) -> None:
    for email in ("carol@example.com", "alice@example.com", "bob@example.com"):
        assert invoke("create", email, "-p", GOOD_PASSWORD).exit_code == 0

    listed = [line.split()[0] for line in invoke("list").stdout.splitlines() if line.strip()]
    assert listed == ["alice@example.com", "bob@example.com", "carol@example.com"]


def test_an_expired_session_is_not_counted(
    invoke, user: User, session_factory: sessionmaker[Session]
) -> None:
    """A token nobody has tried to use yet is still in the table, but it is not a session."""
    add_refresh_token(session_factory, user, expires_in=-1)
    row = next(line for line in invoke("list").stdout.splitlines() if line.startswith(EMAIL))
    assert row.split()[2] == "0"


def test_the_header_stays_off_stdout(invoke, user: User) -> None:
    """So `inkspire user list | cut -d" " -f1` gives addresses and nothing else."""
    result = invoke("list")
    assert "EMAIL" not in result.stdout
    assert "EMAIL" in result.stderr


# --- user create -----------------------------------------------------------


def test_creates_an_account_with_an_explicit_password(invoke, session: Session) -> None:
    result = invoke("create", NEW_EMAIL, "--password", GOOD_PASSWORD)
    assert result.exit_code == 0, result.output

    account = find(session, NEW_EMAIL)
    assert account is not None
    assert verify_password(GOOD_PASSWORD, account.password)
    assert account.all_roles() == ["ROLE_USER"]


def test_generates_and_prints_a_password_when_omitted(invoke, session: Session) -> None:
    result = invoke("create", NEW_EMAIL)
    assert result.exit_code == 0, result.output

    match = re.search(r"Generated password: ([0-9a-f]{24})", result.output)
    assert match, result.output

    account = find(session, NEW_EMAIL)
    assert account is not None
    assert verify_password(match.group(1), account.password)


def test_grants_the_requested_roles(invoke, session: Session) -> None:
    result = invoke("create", NEW_EMAIL, "--password", GOOD_PASSWORD, "--role", "ROLE_ADMIN")
    assert result.exit_code == 0, result.output

    account = find(session, NEW_EMAIL)
    assert account is not None
    assert account.roles == ["ROLE_ADMIN"]
    assert account.all_roles() == ["ROLE_ADMIN", "ROLE_USER"]


def test_a_lowercase_role_is_accepted_and_upcased(invoke, session: Session) -> None:
    assert invoke("create", NEW_EMAIL, "-p", GOOD_PASSWORD, "-r", "role_admin").exit_code == 0
    account = find(session, NEW_EMAIL)
    assert account is not None
    assert account.roles == ["ROLE_ADMIN"]


def test_a_duplicate_email_is_rejected_and_changes_nothing(invoke, session: Session) -> None:
    assert invoke("create", NEW_EMAIL, "--password", GOOD_PASSWORD).exit_code == 0

    result = invoke("create", NEW_EMAIL, "--password", "another-password")
    assert result.exit_code == 1
    assert "already exists" in result.output

    account = find(session, NEW_EMAIL)
    assert account is not None
    assert verify_password(GOOD_PASSWORD, account.password)


def test_an_invalid_email_is_rejected(invoke, session: Session) -> None:
    result = invoke("create", "not-an-email", "--password", GOOD_PASSWORD)
    assert result.exit_code == 1
    assert "not a valid email address" in result.output
    assert find(session, "not-an-email") is None


def test_a_short_password_is_rejected(invoke, session: Session) -> None:
    result = invoke("create", NEW_EMAIL, "--password", "abc")
    assert result.exit_code == 1
    assert "at least 6 characters" in result.output
    assert find(session, NEW_EMAIL) is None


def test_a_malformed_role_is_rejected(invoke, session: Session) -> None:
    result = invoke("create", NEW_EMAIL, "--password", GOOD_PASSWORD, "--role", "admin")
    assert result.exit_code == 1
    assert "must start with ROLE_" in result.output
    assert find(session, NEW_EMAIL) is None


def test_an_overlong_email_is_rejected(invoke, session: Session) -> None:
    """The column holds 180 characters; the CLI must not hand it more."""
    long_email = "a" * 175 + "@example.com"
    result = invoke("create", long_email, "--password", GOOD_PASSWORD)
    assert result.exit_code == 1
    assert "at most 180 characters" in result.output


def test_a_long_password_is_accepted(invoke, client: TestClient) -> None:
    """The policy allows up to 4096 characters, well past what bcrypt hashes."""
    long_password = "correct horse battery staple " * 8
    assert invoke("create", NEW_EMAIL, "--password", long_password).exit_code == 0
    assert (
        client.post("/auth", json={"username": NEW_EMAIL, "password": long_password}).status_code
        == 200
    )


def test_a_created_account_can_log_in(invoke, client: TestClient) -> None:
    """An account written by the command authenticates against the running API."""
    assert invoke("create", NEW_EMAIL, "--password", GOOD_PASSWORD).exit_code == 0

    response = client.post("/auth", json={"username": NEW_EMAIL, "password": GOOD_PASSWORD})
    assert response.status_code == 200
    assert response.json()["token"]


# --- user reset-password ---------------------------------------------------


def test_sets_an_explicit_password(invoke, user: User, session: Session) -> None:
    result = invoke("reset-password", EMAIL, "--password", "brand-new-password")
    assert result.exit_code == 0, result.output

    account = find(session, EMAIL)
    assert account is not None
    assert verify_password("brand-new-password", account.password)
    assert not verify_password(PASSWORD, account.password)


def test_reset_generates_and_prints_a_password_when_omitted(
    invoke, user: User, session: Session
) -> None:
    result = invoke("reset-password", EMAIL)
    assert result.exit_code == 0, result.output

    match = re.search(r"Generated password: ([0-9a-f]{24})", result.output)
    assert match, result.output

    account = find(session, EMAIL)
    assert account is not None
    assert verify_password(match.group(1), account.password)


def test_reset_revokes_refresh_tokens_by_default(
    invoke, user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    add_refresh_token(session_factory, user, expires_in=3600)

    result = invoke("reset-password", EMAIL, "--password", "brand-new-password")
    assert result.exit_code == 0, result.output
    assert "Refresh tokens revoked: 1" in result.output

    session.expire_all()
    assert session.scalars(select(RefreshToken)).all() == []


def test_keep_sessions_leaves_refresh_tokens_intact(
    invoke, user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    add_refresh_token(session_factory, user, expires_in=3600)

    result = invoke("reset-password", EMAIL, "--password", "brand-new-password", "--keep-sessions")
    assert result.exit_code == 0, result.output
    assert "left active" in result.output

    session.expire_all()
    assert len(session.scalars(select(RefreshToken)).all()) == 1


def test_reset_on_an_unknown_email_fails(invoke) -> None:
    result = invoke("reset-password", "nobody@example.com", "--password", GOOD_PASSWORD)
    assert result.exit_code == 1
    assert "No account found" in result.output


def test_reset_rejects_a_short_password(invoke, user: User, session: Session) -> None:
    result = invoke("reset-password", EMAIL, "--password", "abc")
    assert result.exit_code == 1
    assert "at least 6 characters" in result.output

    account = find(session, EMAIL)
    assert account is not None
    assert verify_password(PASSWORD, account.password), "the old password must still work"


def test_a_reset_invalidates_the_old_password_over_http(
    invoke, user: User, client: TestClient
) -> None:
    assert invoke("reset-password", EMAIL, "--password", "brand-new-password").exit_code == 0

    assert client.post("/auth", json={"username": EMAIL, "password": PASSWORD}).status_code == 401
    assert (
        client.post("/auth", json={"username": EMAIL, "password": "brand-new-password"}).status_code
        == 200
    )


def test_a_revoked_refresh_token_cannot_be_used_after_a_reset(
    invoke, logged_in: TestClient
) -> None:
    """Revocation has to bite at the endpoint, not only in the table."""
    assert invoke("reset-password", EMAIL, "--password", "brand-new-password").exit_code == 0
    assert logged_in.post("/auth/refresh").status_code == 401


# --- llm -------------------------------------------------------------------


TEXT = "The lantern guttered as she reached the top of the stairs. Below her, the house was"


@pytest.fixture
def ask(monkeypatch, tmp_path):
    """Runs an `llm` command against a provider answered by a handler, not the network.

    The returned callable takes the handler, the arguments, and what to put on standard
    input, and hands back both the click result and the request bodies the provider saw.
    """
    def run(
        handler,
        *args: str,
        stdin: str = "",
        protocol: str = "openai",
        small_model: str | None = None,
    ):
        seen: list[dict] = []

        def record(request: httpx.Request) -> httpx.Response:
            if request.content:
                seen.append(json.loads(request.content))
            return handler(request)

        settings = llm_settings(tmp_path, protocol=protocol, small_model=small_model)
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(
            cli,
            "LLMService",
            lambda given: LLMService(given, transport=httpx.MockTransport(record)),
        )
        result = runner.invoke(cli.app, ["llm", *args], input=stdin)
        return result, seen

    return run


def test_models_are_listed_one_per_line(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]}),
        "models",
    )
    assert result.exit_code == 0, result.output
    assert result.output.split() == ["p/a", "p/b"]


def test_listing_no_models_is_an_error(ask) -> None:
    """An empty list means nothing is configured or nothing answered; both need saying."""
    result, _ = ask(lambda request: httpx.Response(200, json={"data": []}), "models")
    assert result.exit_code == 1
    assert "No models" in result.output


def test_generate_writes_the_continuation_to_stdout(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=sse_body(delta(" bathed"), delta(" in light."))),
        "generate",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(" bathed in light.")


def test_generate_sends_the_text_from_stdin_inside_the_prompt(ask) -> None:
    _, seen = ask(
        lambda request: httpx.Response(200, text=sse_body(delta("x"))),
        "generate",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert seen[0]["messages"][0]["content"] == render_prompt(TEXT)


def test_generate_reports_timings_on_stderr(ask) -> None:
    """Timings must not land in the continuation, which is meant to be redirectable."""
    result, _ = ask(
        lambda request: httpx.Response(200, text=sse_body(delta("abc"))),
        "generate",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert "characters" not in result.stdout
    assert "3 characters" in result.stderr


def test_no_think_reaches_the_provider(ask) -> None:
    """The whole point of the flag: a reasoning model has to be told not to think."""
    _, seen = ask(
        lambda request: httpx.Response(200, text='{"message":{"content":"x"},"done":true}\n'),
        "generate",
        "-m",
        "p/model",
        "--no-think",
        stdin=TEXT,
        protocol="ollama",
    )
    assert seen[0]["think"] is False


def test_think_reaches_the_provider(ask) -> None:
    _, seen = ask(
        lambda request: httpx.Response(200, text='{"message":{"content":"x"},"done":true}\n'),
        "generate",
        "-m",
        "p/model",
        "--think",
        stdin=TEXT,
        protocol="ollama",
    )
    assert seen[0]["think"] is True


def test_show_prompt_generates_nothing(ask) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, seen = ask(handler, "generate", "-m", "p/model", "--show-prompt", stdin=TEXT)
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip("\n") == render_prompt(TEXT)
    assert seen == []


def test_show_prompt_with_a_cursor_renders_fill_in_the_middle(ask) -> None:
    """#14: `--cursor` is a flat offset into stdin, converted the same way the API
    would resolve one reported against a paragraph and an offset."""
    offset = TEXT.index("Below")

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, seen = ask(
        handler, "generate", "-m", "p/model", "--show-prompt", "--cursor", str(offset), stdin=TEXT
    )
    assert result.exit_code == 0, result.output
    at_cursor = prompt_lib.cursor_from_offset(TEXT, offset)
    assert result.stdout.rstrip("\n") == render_prompt(TEXT, cursor=at_cursor)
    assert "Text before:" in result.stdout
    assert "Text after:" in result.stdout
    assert seen == []


def test_show_prompt_with_a_select_renders_a_rewrite(ask) -> None:
    """#20: `--select` is two flat offsets into stdin, each resolved the same way
    `--cursor` resolves one. With neither `--send-selection` nor `--no-send-selection`
    given, the server's own setting applies -- on by default -- so the passage itself
    renders."""
    start = TEXT.index("Below")
    end = start + len("Below")

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, seen = ask(
        handler,
        "generate",
        "-m",
        "p/model",
        "--show-prompt",
        "--select",
        f"{start}:{end}",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    at_selection = prompt_lib.CursorRange(
        start=prompt_lib.cursor_from_offset(TEXT, start),
        end=prompt_lib.cursor_from_offset(TEXT, end),
    )
    assert result.stdout.rstrip("\n") == render_prompt(TEXT, selection=at_selection)
    assert "Passage to replace:" in result.stdout
    assert "Below" in result.stdout
    assert seen == []


def test_show_prompt_with_a_select_and_no_send_selection_withholds_the_passage(
    ask,
) -> None:
    """`--no-send-selection` overrides the server's own (on) default, so the bare
    marker renders instead of the passage."""
    start = TEXT.index("Below")
    end = start + len("Below")

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, seen = ask(
        handler,
        "generate",
        "-m",
        "p/model",
        "--show-prompt",
        "--select",
        f"{start}:{end}",
        "--no-send-selection",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    at_selection = prompt_lib.CursorRange(
        start=prompt_lib.cursor_from_offset(TEXT, start),
        end=prompt_lib.cursor_from_offset(TEXT, end),
    )
    assert result.stdout.rstrip("\n") == render_prompt(
        TEXT, selection=at_selection, send_selection=False
    )
    assert "[REPLACE THIS]" in result.stdout
    assert "Below" not in result.stdout
    assert seen == []


def test_cursor_and_select_together_is_an_error(ask) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, seen = ask(
        handler,
        "generate",
        "-m",
        "p/model",
        "--show-prompt",
        "--cursor",
        "5",
        "--select",
        "0:5",
        stdin=TEXT,
    )
    assert result.exit_code != 0
    assert seen == []


def test_empty_input_is_an_error(ask) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, _ = ask(handler, "generate", "-m", "p/model", stdin="   \n")
    assert result.exit_code == 1
    assert "No text on standard input" in result.output


def test_an_unknown_model_is_reported_without_a_traceback(ask) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    result, _ = ask(handler, "generate", "-m", "bare-name", stdin=TEXT)
    assert result.exit_code == 1
    assert "prefixed" in result.output


def test_a_provider_failure_is_reported_without_a_traceback(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(503, text="down"),
        "generate",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert result.exit_code == 1
    assert "503" in result.output


def test_a_provider_that_writes_nothing_is_an_error(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=sse_body()),
        "generate",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert result.exit_code == 1
    assert "no text" in result.output


# --- llm title ---------------------------------------------------------------


def test_title_show_prompt_asks_nothing(ask) -> None:
    result, seen = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "--show-prompt",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    assert "Propose a title" in result.output
    assert TEXT in result.output
    assert not seen


def test_title_current_title_reaches_the_prompt(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "--current-title",
        "The Wax Still Held",
        "--show-prompt",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    assert 'Its current title is "The Wax Still Held"' in result.output


def test_title_with_no_current_title_mentions_none(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "--show-prompt",
        stdin=TEXT,
    )
    assert "current title" not in result.output


def test_title_instruction_reaches_the_prompt(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "--instruction",
        "make it ominous",
        "--show-prompt",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    assert "The writer adds: make it ominous" in result.output


def test_title_with_no_instruction_mentions_none(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "--show-prompt",
        stdin=TEXT,
    )
    assert "writer adds" not in result.output


def test_title_writes_the_cleaned_title_to_stdout(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body('"The Wax Still Held"')),
        "title",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "The Wax Still Held"


def test_title_sends_the_text_from_stdin_inside_the_prompt(ask) -> None:
    _, seen = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),
        "title",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert TEXT in seen[0]["messages"][0]["content"]
    assert seen[0]["stream"] is False


def test_title_falls_back_to_the_configured_small_model(ask) -> None:
    result, seen = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),
        "title",
        stdin=TEXT,
        small_model="p/small",
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["model"] == "small"


def test_title_with_no_model_given_and_none_configured_is_an_error(ask) -> None:
    result, seen = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        stdin=TEXT,
    )
    assert result.exit_code == 1
    assert "INKSPIRE_LLM_SMALL_MODEL" in result.output
    assert not seen


def test_title_empty_input_is_an_error(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "-m",
        "p/model",
        stdin="   ",
    )
    assert result.exit_code == 1
    assert "No text" in result.output


def test_title_an_unknown_model_is_reported_without_a_traceback(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("A Title")),  # pragma: no cover
        "title",
        "-m",
        "absent/model",
        stdin=TEXT,
    )
    assert result.exit_code == 1
    assert "absent" in result.output


def test_title_with_nothing_usable_is_an_error(ask) -> None:
    result, _ = ask(
        lambda request: httpx.Response(200, text=answer_body("   ")),
        "title",
        "-m",
        "p/model",
        stdin=TEXT,
    )
    assert result.exit_code == 1
    assert "nothing usable" in result.output


# --- ink check -------------------------------------------------------------


@pytest.fixture
def check(monkeypatch, tmp_path):
    """Runs `inkspire ink check`, with both roots where the test put them."""
    data_root = tmp_path / "novel-data"
    (data_root / "stories").mkdir(parents=True)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            data_root=data_root, files_root=tmp_path / "files", jwt_secret="s" * 32
        ),
    )

    def run(*args: str):
        return runner.invoke(cli.app, ["ink", "check", *args])

    return run


def write_chapter(root: Path, name: str, text: str) -> Path:
    """One `.ink` file in a story, which is all the checker looks at."""
    chapters = root / "novel-data" / "stories" / "example-story" / "chapters"
    chapters.mkdir(parents=True, exist_ok=True)
    path = chapters / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_repository_of_good_files_passes(check, tmp_path) -> None:
    write_chapter(tmp_path, "one.ink", "===== ink:meta\ntitle: One\n===== ink:body\nOnce.\n")
    write_chapter(tmp_path, "two.ink", "Prose with no header.\n")

    result = check()

    assert result.exit_code == 0
    assert "2 files checked, 0 errors, 0 warnings." in result.output


def test_prose_above_the_first_section_fails_and_says_where(check, tmp_path) -> None:
    write_chapter(tmp_path, "one.ink", "Once.\n===== ink:body\nTwice.\n")

    result = check()

    assert result.exit_code == 1
    assert "one.ink:1: error:" in result.output
    assert "1 file checked, 1 error, 0 warnings." in result.output


def test_a_warning_alone_passes(check, tmp_path) -> None:
    """A key nothing reads is worth saying, and is not worth failing over."""
    write_chapter(tmp_path, "one.ink", "===== ink:meta\ntitle: One\npov: Jane Doe\n===== ink:body\nOnce.\n")

    result = check()

    assert result.exit_code == 0
    assert 'the key "pov" means nothing here' in result.output
    assert "1 file checked, 0 errors, 1 warning." in result.output


def test_a_file_that_is_not_utf8_is_an_error(check, tmp_path) -> None:
    chapters = tmp_path / "novel-data" / "stories" / "example-story" / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "one.ink").write_bytes(b"===== ink:meta\ntitle: \xff\xfe\n===== ink:body\n")

    result = check()

    assert result.exit_code == 1
    assert "cannot be read as text" in result.output


def test_one_named_file_is_checked_on_its_own(check, tmp_path) -> None:
    good = write_chapter(tmp_path, "one.ink", "===== ink:meta\ntitle: One\n===== ink:body\nOnce.\n")
    write_chapter(tmp_path, "two.ink", "Once.\n===== ink:body\nTwice.\n")

    result = check(str(good))

    assert result.exit_code == 0
    assert "1 file checked" in result.output


def test_an_empty_repository_has_nothing_to_check(check) -> None:
    result = check()

    assert result.exit_code == 0
    assert "No .ink files to check." in result.output


def test_a_path_that_is_not_there_is_refused(check, tmp_path) -> None:
    result = check(str(tmp_path / "gone.ink"))

    assert result.exit_code == 1
    assert "is not there" in result.output


def test_both_roots_are_checked(check, tmp_path) -> None:
    """A note is the same kind of file as a chapter, and is checked with them."""
    write_chapter(tmp_path, "one.ink", "===== ink:meta\ntitle: One\n===== ink:body\nOnce.\n")
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "scratch.ink").write_text(
        "A list.\n===== ink:body\nAnother.\n", encoding="utf-8"
    )

    result = check()

    assert result.exit_code == 1
    assert "scratch.ink:1: error:" in result.output
    assert "2 files checked, 1 error, 0 warnings." in result.output


def test_a_root_that_is_not_there_is_not_an_error(check, tmp_path) -> None:
    write_chapter(tmp_path, "one.ink", "Once.\n")

    result = check()

    assert result.exit_code == 0
    assert "1 file checked" in result.output


# --- ink reclassify ---------------------------------------------------------

OLD_PARA = "The door creaked. The streets glistened like wet glass under the lamplight."
NEW_PARA = "The door creaked open. The streets glistened like wet glass under the lamplight."
OLD_KEY = "47f57caaa4fb330e"
NEW_KEY = "39f4ef4afdf22890"
OLD_SECTION = f'"{OLD_KEY}": [[18, 45, "gen"], [45, 54, "fix"], [54, 75, "gen"]]\n'
CHAPTER_PATH = "stories/example-story/chapters/one.ink"


def ink_file(body: str, section: str | None = None) -> str:
    """One `.ink` file's text, with a provenance footer where there is one."""
    text = f"===== ink:body\n{body}\n"
    return text if section is None else f"{text}===== ink:provenance\n{section}"


@pytest.fixture
def reclassify(monkeypatch, tmp_path):
    """Runs `inkspire ink reclassify`, with both roots where the test put them."""
    data_root = tmp_path / "novel-data"
    # exist_ok, because `story_repo` may have made it first: both fixtures build the
    # same root and nothing fixes which one pytest resolves first.
    (data_root / "stories").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            data_root=data_root, files_root=tmp_path / "files", jwt_secret="s" * 32
        ),
    )

    def run(*args: str):
        return runner.invoke(cli.app, ["ink", "reclassify", *args])

    return run


@pytest.fixture
def story_repo(tmp_path):
    """A story repository with one chapter holding §7.5's paragraph and its provenance,
    committed, so there is a history to recover from."""
    data_root = tmp_path / "novel-data"
    chapters = data_root / "stories" / "example-story" / "chapters"
    chapters.mkdir(parents=True, exist_ok=True)
    (chapters / "one.ink").write_text(ink_file(OLD_PARA, OLD_SECTION), encoding="utf-8")

    repo = Repo.init(data_root, initial_branch="main")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Jane Doe")
        writer.set_value("user", "email", "jane@example.com")
    repo.index.add([CHAPTER_PATH])
    repo.index.commit("Add the chapter")
    return data_root


def edit(data_root: Path, body: str, section: str | None = OLD_SECTION) -> Path:
    """Rewrites the chapter the way a hand edit outside the editor would."""
    path = data_root / CHAPTER_PATH
    path.write_text(ink_file(body, section), encoding="utf-8")
    return path


def test_a_dry_run_writes_nothing_and_exits_non_zero(reclassify, story_repo) -> None:
    path = edit(story_repo, NEW_PARA)
    before = path.read_bytes()

    result = reclassify()

    assert result.exit_code == 1
    assert path.read_bytes() == before
    assert "STALE" in result.output
    assert "recoverable" in result.output
    assert "Nothing written; pass --force to apply." in result.output


def test_a_dry_run_is_the_default_and_may_be_asked_for(reclassify, story_repo) -> None:
    """So intent is visible in a script."""
    path = edit(story_repo, NEW_PARA)
    before = path.read_bytes()

    result = reclassify("--dry-run")

    assert result.exit_code == 1
    assert path.read_bytes() == before


def test_force_rewrites_the_entry_under_the_new_hash(reclassify, story_repo) -> None:
    path = edit(story_repo, NEW_PARA)

    result = reclassify("--force")

    assert result.exit_code == 0
    text = path.read_text(encoding="utf-8")
    assert f'"{NEW_KEY}": [[23, 50, "gen"], [50, 59, "fix"], [59, 80, "gen"]]' in text
    assert OLD_KEY not in text
    assert NEW_PARA in text
    assert "1 file written." in result.output


def test_force_leaves_nothing_stale_behind(reclassify, story_repo) -> None:
    """A second run has nothing to do, which is what says the first one worked."""
    edit(story_repo, NEW_PARA)
    assert reclassify("-f").exit_code == 0

    second = reclassify()
    assert second.exit_code == 0
    assert "STALE" not in second.output


def test_force_and_dry_run_together_are_refused(reclassify, story_repo) -> None:
    edit(story_repo, NEW_PARA)
    result = reclassify("--force", "--dry-run")
    assert result.exit_code == 1
    assert "not both" in result.output


def test_a_clean_file_is_not_reported_at_all(reclassify, story_repo) -> None:
    """Nothing is stale, so there is nothing to say about it."""
    result = reclassify()

    assert result.exit_code == 0
    assert "STALE" not in result.output
    assert "1 file checked, 0 stale" in result.output


def test_a_file_with_no_provenance_is_left_alone(reclassify, tmp_path) -> None:
    path = write_chapter(tmp_path, "one.ink", ink_file("Once."))
    before = path.read_bytes()

    result = reclassify("--force")

    assert result.exit_code == 0
    assert path.read_bytes() == before


def test_no_matching_revision_resets_that_paragraph_only(reclassify, story_repo) -> None:
    """Its neighbour keeps its runs exactly, which is why provenance is keyed by
    paragraph and not by file."""
    kept = "Untouched."
    kept_key = provenance.paragraph_hash(kept)
    edit(
        story_repo,
        f"{kept}\n\nNothing whatsoever to do with any of that.",
        f'"{kept_key}": [[0, 2, "fix"]]\n"deadbeefdeadbeef": [[0, 4, "gen"]]\n',
    )

    result = reclassify("--force")

    assert result.exit_code == 0
    text = (story_repo / CHAPTER_PATH).read_text(encoding="utf-8")
    assert f'"{kept_key}": [[0, 2, "fix"]]' in text
    assert "deadbeefdeadbeef" not in text
    assert "would reset" in result.output


def test_a_notes_file_resets_without_attempting_git(reclassify, tmp_path) -> None:
    """The notes root is not a repository, so there is nothing to recover from."""
    files_root = tmp_path / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    path = files_root / "scratch.ink"
    path.write_text(ink_file(NEW_PARA, OLD_SECTION), encoding="utf-8")

    result = reclassify("--force")

    assert result.exit_code == 0
    text = path.read_text(encoding="utf-8")
    assert f'"{NEW_KEY}": []' in text
    assert OLD_KEY not in text
    assert "would reset" in result.output


def test_a_story_root_that_is_not_a_repository_can_only_reset(reclassify, tmp_path) -> None:
    """A data root nobody has cloned. The command still works; recovery is what is lost."""
    path = write_chapter(tmp_path, "one.ink", ink_file(NEW_PARA, OLD_SECTION))

    result = reclassify("--force")

    assert result.exit_code == 0
    assert f'"{NEW_KEY}": []' in path.read_text(encoding="utf-8")


def test_one_named_file_is_reclassified_on_its_own(reclassify, story_repo) -> None:
    path = edit(story_repo, NEW_PARA)
    other = story_repo / "stories" / "example-story" / "chapters" / "two.ink"
    other.write_text(ink_file(NEW_PARA, OLD_SECTION), encoding="utf-8")

    result = reclassify("-f", str(path))

    assert result.exit_code == 0
    assert "1 file checked" in result.output
    assert OLD_KEY in other.read_text(encoding="utf-8")


def test_a_section_that_cannot_be_read_is_left_alone(reclassify, story_repo) -> None:
    """There is no recovery to apply to a section nobody can parse, and rewriting it
    from nothing would throw away whatever it was meant to say."""
    path = edit(story_repo, NEW_PARA, "[unclosed\n")
    before = path.read_bytes()

    result = reclassify("--force")

    assert result.exit_code == 0
    assert path.read_bytes() == before


def test_an_empty_repository_has_nothing_to_reclassify(reclassify) -> None:
    result = reclassify()
    assert result.exit_code == 0
    assert "No .ink files to reclassify." in result.output


def test_a_path_that_is_not_there_is_refused(reclassify, tmp_path) -> None:
    result = reclassify(str(tmp_path / "gone.ink"))
    assert result.exit_code == 1
    assert "is not there" in result.output


def test_the_report_names_the_paragraph_and_the_hash(reclassify, story_repo) -> None:
    edit(story_repo, NEW_PARA)

    result = reclassify()

    assert "para 1" in result.output
    assert "39f4…2890" in result.output
    assert f"was {OLD_KEY[:4]}…{OLD_KEY[-4:]}" in result.output
