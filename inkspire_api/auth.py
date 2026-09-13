# -*- coding: utf-8 -*-
"""`POST /auth`, `POST /auth/refresh`, `POST /auth/logout`.

A login mints a short-lived JWT and a long-lived refresh token, both as cookies.
The refresh token is rotated on every use, so a captured one stops working as soon
as the account's own client refreshes with its copy.

Login also returns the JWT in the body, for clients that send it as a header
instead of holding cookies.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from .deps import SessionDep, SettingsDep
from .models import RefreshToken, User, new_refresh_token, utcnow
from .security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    create_jwt,
    is_secure,
    set_auth_cookies,
    verify_password,
)

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    #: The account's email address. The field is named `username` because that is
    #: what clients send.
    username: str
    password: str


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _expiry(seconds: int) -> datetime.datetime:
    return utcnow() + datetime.timedelta(seconds=seconds)


@router.post("/auth")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict:
    throttle = request.app.state.login_throttle
    key = _client_key(request)
    if throttle.blocked(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed login attempts; please try again later.",
        )

    user = session.scalar(select(User).where(User.email == payload.username))
    # An unknown email skips bcrypt, so a failed attempt against a non-existent
    # account returns measurably faster than one against a real account. Accounts
    # are created from the shell and cannot be enumerated any other way, so the
    # cost of hashing a dummy password on every miss is not worth paying.
    if user is None or not verify_password(payload.password, user.password):
        throttle.record_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials.")

    throttle.reset(key)

    refresh_token = new_refresh_token()
    session.add(
        RefreshToken(
            token=refresh_token,
            user=user,
            expires_at=_expiry(settings.refresh_token_ttl),
        )
    )
    session.commit()

    token = create_jwt(user, settings)
    set_auth_cookies(
        response,
        jwt_token=token,
        refresh_token=refresh_token,
        secure=is_secure(request),
        settings=settings,
    )
    return {"token": token}


@router.post("/auth/refresh")
def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict:
    """Issues a fresh JWT and rotates the refresh token in place."""
    presented = request.cookies.get(REFRESH_COOKIE)
    if not presented:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")

    stored = session.scalar(select(RefreshToken).where(RefreshToken.token == presented))
    if stored is None or stored.is_expired():
        if stored is not None:
            session.delete(stored)
            session.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token"
        )

    user = stored.user
    # Rotation overwrites the row, so one session is always one row and the previous
    # token stops working immediately.
    stored.token = new_refresh_token()
    stored.expires_at = _expiry(settings.refresh_token_ttl)
    session.commit()

    set_auth_cookies(
        response,
        jwt_token=create_jwt(user, settings),
        refresh_token=stored.token,
        secure=is_secure(request),
        settings=settings,
    )
    return {"success": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response, session: SessionDep) -> dict:
    """Revokes the refresh token and clears the cookies.

    Succeeds whether or not a session was live, so a client can call it to clean up
    without first checking.
    """
    presented = request.cookies.get(REFRESH_COOKIE)
    if presented:
        stored = session.scalar(
            select(RefreshToken).where(RefreshToken.token == presented)
        )
        if stored is not None:
            session.delete(stored)
            session.commit()

    clear_auth_cookies(response, secure=is_secure(request))
    return {"success": True}
