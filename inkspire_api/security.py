# -*- coding: utf-8 -*-
"""Password hashing, JWT minting, and the three auth cookies.

The cookie names, paths and flags are part of the HTTP contract the browser client
depends on. Changing any of them logs every session out.
"""

from __future__ import annotations

import datetime

import bcrypt
import jwt
from fastapi import Request, Response

from .models import User
from .settings import Settings

ALGORITHM = "HS256"

JWT_COOKIE = "jwt_token"
REFRESH_COOKIE = "refresh_token"
STATUS_COOKIE = "auth_status"

#: The refresh cookie is scoped to /auth so it is not sent on every /api call.
REFRESH_COOKIE_PATH = "/auth"

#: bcrypt hashes at most 72 bytes and silently ignores the rest.
BCRYPT_MAX_BYTES = 72


def hash_password(password: str, rounds: int) -> str:
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Checks a password against a stored hash of any bcrypt variant (`$2a$`, `$2b$`, `$2y$`)."""
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("ascii"))
    except (ValueError, TypeError):  # a malformed or non-bcrypt hash never matches
        return False


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def create_jwt(user: User, settings: Settings) -> str:
    """Mints an access token. `username` carries the email, which identifies the account."""
    issued = datetime.datetime.now(datetime.UTC)
    payload = {
        "iat": int(issued.timestamp()),
        "exp": int(issued.timestamp()) + settings.jwt_ttl,
        "username": user.email,
        "roles": user.all_roles(),
    }
    return jwt.encode(payload, settings.jwt_secret_or_raise(), algorithm=ALGORITHM)


def decode_jwt(token: str, settings: Settings) -> dict:
    """Returns the claims. Raises `jwt.InvalidTokenError`, of which expiry is a subclass."""
    return jwt.decode(token, settings.jwt_secret_or_raise(), algorithms=[ALGORITHM])


def is_secure(request: Request) -> bool:
    return request.url.scheme == "https"


def set_auth_cookies(
    response: Response,
    *,
    jwt_token: str,
    refresh_token: str,
    secure: bool,
    settings: Settings,
) -> None:
    """Writes all three cookies. `auth_status` is the only one JS can read."""
    response.set_cookie(
        JWT_COOKIE,
        jwt_token,
        max_age=settings.jwt_ttl,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.refresh_token_ttl,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        STATUS_COOKIE,
        "1",
        max_age=settings.refresh_token_ttl,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_auth_cookies(response: Response, *, secure: bool) -> None:
    """Expires all three cookies, each on the path it was set with."""
    response.delete_cookie(
        JWT_COOKIE, path="/", secure=secure, httponly=True, samesite="strict"
    )
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        STATUS_COOKIE, path="/", secure=secure, httponly=False, samesite="strict"
    )
