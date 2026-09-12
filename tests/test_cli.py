# -*- coding: utf-8 -*-
"""`inkspire user create` and `inkspire user reset-password`.

Several tests end at the login endpoint rather than at the hasher: what matters
about these commands is that the account they write can actually be used, and that
a reset actually invalidates what came before.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from conftest import EMAIL, PASSWORD, add_refresh_token

from inkspire_api import cli
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
