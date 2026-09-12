# -*- coding: utf-8 -*-
"""The commands: `inkspire user …` and `inkspire llm …`.

Several account tests end at the login endpoint rather than at the hasher: what
matters about those commands is that the account they write can actually be used, and
that a reset actually invalidates what came before.

The generation tests check what reaches the provider and where output goes, since the
command exists to exercise the same prompt and providers the API serves.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from conftest import (
    EMAIL,
    PASSWORD,
    add_refresh_token,
    delta,
    llm_settings,
    sse_body,
)

from inkspire_api import cli
from inkspire_api.llm import LLMService, render_prompt
from inkspire_api.models import RefreshToken, User
from inkspire_api.security import verify_password
from inkspire_api.settings import Settings

NEW_EMAIL = "new-user@example.com"
GOOD_PASSWORD = "a-good-password"

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

    def run(*args: str, secret: str | None = "s" * 32, providers: bool = True):
        settings = llm_settings(tmp_path) if providers else Settings(
            llm_providers_file=tmp_path / "absent.yaml"
        )
        settings = settings.model_copy(update={"jwt_secret": secret})
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
    def run(handler, *args: str, stdin: str = "", protocol: str = "openai"):
        seen: list[dict] = []

        def record(request: httpx.Request) -> httpx.Response:
            if request.content:
                seen.append(json.loads(request.content))
            return handler(request)

        settings = llm_settings(tmp_path, protocol=protocol)
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
