# -*- coding: utf-8 -*-
"""Request dependencies: the session, the settings, and the authenticated user.

`current_user` is what makes a route private. It is declared once on the `/api`
router rather than per route, so a new route cannot be left public by omission.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_session
from .models import User
from .security import JWT_COOKIE, decode_jwt
from .settings import Settings, get_settings

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def bearer_token(request: Request) -> str | None:
    """The JWT from the Authorization header, else from the jwt_token cookie.

    The browser client uses the cookie, which it cannot read; other API clients use
    the header. Both are accepted everywhere.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header.removeprefix("Bearer ").strip() or None
    return request.cookies.get(JWT_COOKIE)


def current_user(
    request: Request, session: SessionDep, settings: SettingsDep
) -> User:
    token = bearer_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "JWT Token not found")

    try:
        claims = decode_jwt(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Expired JWT Token") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid JWT Token") from None

    email = claims.get("username")
    user = session.scalar(select(User).where(User.email == email)) if email else None
    if user is None:
        # A token signed for an account that has since been deleted.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid JWT Token")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
