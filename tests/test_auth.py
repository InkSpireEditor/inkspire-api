# -*- coding: utf-8 -*-
"""Login, refresh rotation, logout, and access control on /api.

The cookie names, paths and flags asserted here are the ones the browser client
reads, so a change that breaks one of these assertions breaks that client.
"""

from __future__ import annotations

import datetime

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import EMAIL, PASSWORD, add_refresh_token, bearer, set_cookies

from inkspire_api.db import get_session
from inkspire_api.main import create_app
from inkspire_api.models import RefreshToken, User
from inkspire_api.security import (
    JWT_COOKIE,
    REFRESH_COOKIE,
    STATUS_COOKIE,
    create_jwt,
    decode_jwt,
    hash_password,
)
from inkspire_api.settings import Settings, get_settings


def count_tokens(session: Session) -> int:
    return len(session.scalars(select(RefreshToken)).all())


# --- POST /auth ------------------------------------------------------------


def test_login_with_an_unknown_email_is_unauthorized(client: TestClient, user: User) -> None:
    response = client.post(
        "/auth", json={"username": "doesNotExist@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401
    assert "message" in response.json()


def test_login_with_a_wrong_password_is_unauthorized(client: TestClient, user: User) -> None:
    response = client.post("/auth", json={"username": EMAIL, "password": "bad-password"})
    assert response.status_code == 401
    assert "message" in response.json()


def test_login_with_valid_credentials_returns_a_token(client: TestClient, user: User) -> None:
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["token"]


def test_a_failed_login_sets_no_cookies(client: TestClient, user: User) -> None:
    response = client.post("/auth", json={"username": EMAIL, "password": "wrong"})
    assert set_cookies(response) == {}


def test_a_body_without_a_username_is_a_bad_request(client: TestClient, user: User) -> None:
    """A 400 in the standard error shape, since the client displays `message`."""
    response = client.post("/auth", json={"password": PASSWORD})
    assert response.status_code == 400
    assert "username" in response.json()["message"]


def test_the_token_carries_the_identity_and_roles(
    client: TestClient, user: User, settings: Settings
) -> None:
    token = client.post(
        "/auth", json={"username": EMAIL, "password": PASSWORD}
    ).json()["token"]
    claims = decode_jwt(token, settings)
    assert claims["username"] == EMAIL
    assert claims["roles"] == ["ROLE_USER"]
    assert claims["exp"] - claims["iat"] == settings.jwt_ttl


def test_login_sets_the_three_cookies_and_persists_one_refresh_token(
    client: TestClient, user: User, session: Session, settings: Settings
) -> None:
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    cookies = set_cookies(response)

    assert set(cookies) == {JWT_COOKIE, REFRESH_COOKIE, STATUS_COOKIE}
    assert count_tokens(session) == 1

    # jwt_token: httpOnly, whole site, strict.
    assert "httponly" in cookies[JWT_COOKIE]
    assert cookies[JWT_COOKIE]["samesite"].lower() == "strict"
    assert cookies[JWT_COOKIE]["path"] == "/"
    assert cookies[JWT_COOKIE]["max-age"] == str(settings.jwt_ttl)

    # refresh_token: httpOnly and scoped to /auth, so it is not sent to /api.
    assert "httponly" in cookies[REFRESH_COOKIE]
    assert cookies[REFRESH_COOKIE]["path"] == "/auth"
    assert cookies[REFRESH_COOKIE]["max-age"] == str(settings.refresh_token_ttl)

    # auth_status: readable by the browser client, which cannot read the other two
    # and has no other way to tell it holds a session.
    assert "httponly" not in cookies[STATUS_COOKIE]
    assert cookies[STATUS_COOKIE]["value"] == "1"
    assert cookies[STATUS_COOKIE]["path"] == "/"


def test_the_refresh_cookie_is_not_sent_to_api_routes(logged_in: TestClient) -> None:
    """Scoping the cookie to /auth keeps the long-lived credential off every API call."""
    sent = logged_in.request("GET", "/api/me").request.headers.get("cookie", "")
    assert JWT_COOKIE in sent
    assert REFRESH_COOKIE not in sent


def test_logging_in_twice_stores_two_tokens(
    client: TestClient, user: User, session: Session
) -> None:
    """Each login is its own session, so signing in elsewhere must not revoke this one."""
    for _ in range(2):
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    assert count_tokens(session) == 2


# --- POST /auth/refresh ----------------------------------------------------


def test_refresh_issues_a_new_jwt_and_rotates_the_token(
    logged_in: TestClient, session: Session
) -> None:
    before = session.scalars(select(RefreshToken)).all()
    assert len(before) == 1
    old_token = before[0].token

    response = logged_in.post("/auth/refresh")
    assert response.status_code == 200
    assert response.json() == {"success": True}

    cookies = set_cookies(response)
    assert set(cookies) == {JWT_COOKIE, REFRESH_COOKIE, STATUS_COOKIE}

    session.expire_all()
    after = session.scalars(select(RefreshToken)).all()
    assert len(after) == 1, "rotation reuses the row"
    assert after[0].token != old_token


def test_refresh_without_a_cookie_is_unauthorized(logged_in: TestClient) -> None:
    logged_in.cookies.clear()
    response = logged_in.post("/auth/refresh")
    assert response.status_code == 401
    assert response.json()["message"] == "No refresh token"


def test_refresh_with_an_unknown_token_is_unauthorized(logged_in: TestClient) -> None:
    logged_in.cookies.clear()
    logged_in.cookies.set(REFRESH_COOKIE, "not-a-real-token", path="/auth")
    response = logged_in.post("/auth/refresh")
    assert response.status_code == 401


def test_refresh_with_an_expired_token_deletes_the_row(
    client: TestClient,
    user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """An expired token is deleted as it is refused, so the table does not accumulate them."""
    expired = add_refresh_token(session_factory, user, expires_in=-1)
    client.cookies.set(REFRESH_COOKIE, expired, path="/auth")

    response = client.post("/auth/refresh")
    assert response.status_code == 401
    assert count_tokens(session) == 0


def test_a_rotated_token_cannot_be_replayed(logged_in: TestClient) -> None:
    used = logged_in.cookies.get(REFRESH_COOKIE, path="/auth")
    assert logged_in.post("/auth/refresh").status_code == 200

    logged_in.cookies.clear()
    logged_in.cookies.set(REFRESH_COOKIE, used, path="/auth")
    assert logged_in.post("/auth/refresh").status_code == 401


def test_the_jwt_from_a_refresh_authenticates(logged_in: TestClient) -> None:
    assert logged_in.post("/auth/refresh").status_code == 200
    assert logged_in.get("/api/me").status_code == 200


# --- POST /auth/logout -----------------------------------------------------


def test_logout_revokes_the_token_and_clears_the_cookies(
    logged_in: TestClient, session: Session
) -> None:
    assert count_tokens(session) == 1

    response = logged_in.post("/auth/logout")
    assert response.status_code == 200

    session.expire_all()
    assert count_tokens(session) == 0

    cookies = set_cookies(response)
    assert set(cookies) == {JWT_COOKIE, REFRESH_COOKIE, STATUS_COOKIE}
    for name, attributes in cookies.items():
        assert attributes["max-age"] == "0", f"{name} must be expired"
        assert attributes["value"] == ""


def test_logout_when_already_logged_out_still_succeeds(client: TestClient) -> None:
    """Clients call logout without checking for a live session first."""
    assert client.post("/auth/logout").status_code == 200


def test_logout_leaves_another_session_alone(
    client: TestClient, user: User, session: Session
) -> None:
    other = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    stale_refresh = set_cookies(other)[REFRESH_COOKIE]["value"]

    client.cookies.clear()
    client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    client.post("/auth/logout")

    session.expire_all()
    remaining = session.scalars(select(RefreshToken)).all()
    assert [row.token for row in remaining] == [stale_refresh]


# --- access control on /api ------------------------------------------------


def test_api_without_credentials_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["message"] == "JWT Token not found"


def test_api_accepts_the_jwt_cookie(logged_in: TestClient) -> None:
    response = logged_in.get("/api/me")
    assert response.status_code == 200
    assert response.json() == {"email": EMAIL, "roles": ["ROLE_USER"]}


def test_api_accepts_the_bearer_header(client: TestClient, user: User) -> None:
    """A client that holds no cookies authenticates with the header instead."""
    headers = bearer(client)
    client.cookies.clear()
    assert client.get("/api/me", headers=headers).status_code == 200


def test_a_garbled_token_is_unauthorized(client: TestClient, user: User) -> None:
    response = client.get("/api/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid JWT Token"


def test_an_expired_jwt_is_unauthorized(
    client: TestClient, user: User, settings: Settings
) -> None:
    """The 401 a client answers by refreshing. Signed correctly, just out of date."""
    expired = create_jwt(user, settings.model_copy(update={"jwt_ttl": -10}))
    response = client.get("/api/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["message"] == "Expired JWT Token"


def test_a_token_signed_with_another_secret_is_unauthorized(
    client: TestClient, user: User, settings: Settings
) -> None:
    forged = jwt.encode(
        {
            "username": EMAIL,
            "roles": ["ROLE_USER"],
            "iat": 0,
            "exp": 2**31,
        },
        "not-the-signing-secret-but-long-enough-for-sha256",
        algorithm="HS256",
    )
    assert client.get("/api/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_a_token_for_a_deleted_account_is_unauthorized(
    client: TestClient, user: User, session_factory: sessionmaker[Session]
) -> None:
    headers = bearer(client)
    client.cookies.clear()
    with session_factory() as session:
        session.delete(session.get(User, user.id))
        session.commit()
    assert client.get("/api/me", headers=headers).status_code == 401


def test_deleting_an_account_deletes_its_refresh_tokens(
    logged_in: TestClient, user: User, session_factory: sessionmaker[Session]
) -> None:
    """ON DELETE CASCADE, which SQLite only honours with the foreign_keys pragma on."""
    with session_factory() as session:
        session.delete(session.get(User, user.id))
        session.commit()
    with session_factory() as session:
        assert count_tokens(session) == 0


# --- throttling ------------------------------------------------------------


@pytest.fixture
def throttled_client(
    settings: Settings, session_factory: sessionmaker[Session], user: User
) -> TestClient:
    """A client whose app throttles after two failures, to keep the test short."""
    throttling = settings.model_copy(
        update={"login_max_attempts": 2, "login_interval": 60}
    )
    app = create_app(throttling)
    app.dependency_overrides[get_settings] = lambda: throttling

    def _session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    return TestClient(app)


def test_login_throttling_blocks_after_the_limit(throttled_client: TestClient) -> None:
    for _ in range(2):
        assert throttled_client.post(
            "/auth", json={"username": EMAIL, "password": "wrong"}
        ).status_code == 401

    blocked = throttled_client.post(
        "/auth", json={"username": EMAIL, "password": PASSWORD}
    )
    assert blocked.status_code == 429
    assert "message" in blocked.json()


def test_a_successful_login_clears_the_failure_count(throttled_client: TestClient) -> None:
    assert throttled_client.post(
        "/auth", json={"username": EMAIL, "password": "wrong"}
    ).status_code == 401
    assert throttled_client.post(
        "/auth", json={"username": EMAIL, "password": PASSWORD}
    ).status_code == 200
    assert throttled_client.post(
        "/auth", json={"username": EMAIL, "password": "wrong"}
    ).status_code == 401
    # Two failures total, but not consecutive, so the limit is not reached.
    assert throttled_client.post(
        "/auth", json={"username": EMAIL, "password": PASSWORD}
    ).status_code == 200


def test_throttling_is_off_in_the_test_configuration(client: TestClient, user: User) -> None:
    """Every other test logs in freely because of this."""
    for _ in range(6):
        assert client.post(
            "/auth", json={"username": EMAIL, "password": "wrong"}
        ).status_code == 401


# --- CORS ------------------------------------------------------------------


def test_a_preflight_from_the_dev_server_is_allowed(client: TestClient) -> None:
    """The browser preflights the cross-origin login; refusing it blocks logging in."""
    response = client.options(
        "/auth",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_a_preflight_from_another_origin_is_not_allowed(client: TestClient) -> None:
    response = client.options(
        "/auth",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


# --- hashing ---------------------------------------------------------------


def test_a_stored_hash_verifies_whatever_its_variant_and_cost(
    client: TestClient, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A hash is verified against the parameters it carries, not the configured ones.

    The hash below is the `$2y$` variant at cost 13, while this suite hashes at cost
    4. Accounts must keep working when either setting changes, with no password reset.
    """
    stored_hash = "$2y$13$Iubga6Z8EV3kAiD7OnZnHu0j3kgxyTUOmChDqYpg/T260cBJPspV2"
    with session_factory() as session:
        session.add(User(email="stored@example.com", roles=[], password=stored_hash))
        session.commit()

    response = client.post(
        "/auth", json={"username": "stored@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_a_password_longer_than_bcrypt_accepts_can_still_be_used(
    client: TestClient, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """bcrypt refuses more than 72 bytes outright, so the input is truncated first.

    Without that, a long passphrase raises on hashing and on every login attempt.
    """
    long_password = "correct horse battery staple " * 4  # 116 bytes
    with session_factory() as session:
        session.add(
            User(
                email="long@example.com",
                roles=[],
                password=hash_password(long_password, settings.bcrypt_rounds),
            )
        )
        session.commit()

    response = client.post(
        "/auth", json={"username": "long@example.com", "password": long_password}
    )
    assert response.status_code == 200


def test_only_the_first_72_bytes_of_a_password_are_checked(
    client: TestClient, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A consequence of that truncation, pinned so it is a known property.

    Two passwords agreeing on their first 72 bytes are the same password here.
    """
    with session_factory() as session:
        session.add(
            User(
                email="prefix@example.com",
                roles=[],
                password=hash_password("y" * 72 + "-original", settings.bcrypt_rounds),
            )
        )
        session.commit()

    response = client.post(
        "/auth", json={"username": "prefix@example.com", "password": "y" * 72 + "-different"}
    )
    assert response.status_code == 200


def test_roles_always_include_role_user(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as session:
        user = User(email="admin@example.com", roles=["ROLE_ADMIN"], password="x")
        assert user.all_roles() == ["ROLE_ADMIN", "ROLE_USER"]
        session.add(user)
        session.commit()


def test_the_refresh_token_is_long_and_random(logged_in: TestClient, session: Session) -> None:
    stored = session.scalars(select(RefreshToken)).all()[0]
    assert len(stored.token) == 64  # 32 bytes, hex encoded
    assert stored.expires_at > datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
