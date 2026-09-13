# -*- coding: utf-8 -*-
"""Accounts and refresh tokens, the only persisted state.

Stories, chapters and their metadata live on the filesystem: the tree comes from a
directory walk, so there is no table for it.
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Bytes of randomness behind a refresh token. Hex-encoded, so the stored string
#: is twice this long and fits the 128-character column.
TOKEN_BYTES = 32

MAX_EMAIL_LENGTH = 180


class Base(DeclarativeBase):
    pass


def new_refresh_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def utcnow() -> datetime.datetime:
    """UTC without a timezone, which is what the DATETIME columns hold."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH), unique=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    password: Mapped[str] = mapped_column(String(255))

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def all_roles(self) -> list[str]:
        """The stored roles plus ROLE_USER, which every account holds implicitly."""
        return list(dict.fromkeys([*self.roles, "ROLE_USER"]))


class RefreshToken(Base):
    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    def is_expired(self) -> bool:
        return self.expires_at <= utcnow()
