# -*- coding: utf-8 -*-
"""Shared fixtures.

Each test gets its own SQLite file under tmp_path, so tests share no state and need
no cleanup step that could leave a row behind.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from inkspire_api.db import get_session
from inkspire_api.main import create_app
from inkspire_api.models import Base, RefreshToken, User, new_refresh_token, utcnow
from inkspire_api.security import hash_password
from inkspire_api.settings import Settings, get_settings

EMAIL = "test@example.com"
PASSWORD = "password"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Test configuration. Explicit arguments win over the environment and `.env`.

    Throttling is off, or a test that logs in repeatedly would start getting 429s.
    `bcrypt_rounds=4` is the lowest bcrypt accepts: a secure work factor is wasted
    here and would dominate the runtime of the suite.
    """
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'data_test.db'}",
        # At least 32 bytes, or PyJWT warns that the HMAC key is short for SHA-256.
        jwt_secret="test-only-signing-secret-padded-to-32-bytes",
        bcrypt_rounds=4,
        login_max_attempts=0,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session for the test itself to inspect rows with."""
    with session_factory() as session:
        yield session


@pytest.fixture
def user(session_factory: sessionmaker[Session], settings: Settings) -> User:
    with session_factory() as session:
        user = User(
            email=EMAIL,
            roles=[],
            password=hash_password(PASSWORD, settings.bcrypt_rounds),
        )
        session.add(user)
        session.commit()
        return user


@pytest.fixture
def app(settings: Settings, session_factory: sessionmaker[Session]):
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings

    def _session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = _session
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def logged_in(client: TestClient, user: User) -> TestClient:
    """A client holding the three cookies of a live session."""
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def bearer(client: TestClient) -> dict[str, str]:
    """The Authorization header for a logged-in client, from a fresh login."""
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['token']}"}


def set_cookies(response) -> dict[str, dict[str, str]]:
    """Every Set-Cookie on a response, as {name: {attribute: value}}.

    Cookie flags are part of the contract, and httpx's cookie jar drops them, so
    the raw headers are parsed instead.
    """
    parsed: dict[str, dict[str, str]] = {}
    for header in response.headers.get_list("set-cookie"):
        first, *rest = [part.strip() for part in header.split(";")]
        name, _, value = first.partition("=")
        # A cleared cookie is sent as `name=""`, quotes included.
        attributes = {"value": value.strip('"')}
        for part in rest:
            key, _, attribute_value = part.partition("=")
            attributes[key.lower()] = attribute_value
        parsed[name] = attributes
    return parsed


def add_refresh_token(
    session_factory: sessionmaker[Session], user: User, *, expires_in: int
) -> str:
    """Stores a refresh token expiring in `expires_in` seconds; a negative value is expired."""
    token = new_refresh_token()
    with session_factory() as session:
        session.add(
            RefreshToken(
                token=token,
                user_id=user.id,
                expires_at=utcnow() + datetime.timedelta(seconds=expires_in),
            )
        )
        session.commit()
    return token
