# -*- coding: utf-8 -*-
"""Engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite ignores foreign keys unless asked, which would strand refresh tokens.

    `refresh_token.user_id` is declared ON DELETE CASCADE; without this pragma
    deleting a user leaves its rows behind.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:  # a non-SQLite driver has no such pragma
        pass
    finally:
        cursor.close()


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding one session per request."""
    with get_sessionmaker()() as session:
        yield session
