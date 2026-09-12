# -*- coding: utf-8 -*-
"""Configuration, read from the environment and from `.env`.

Every key is prefixed `INKSPIRE_`; unprefixed keys in the file are ignored.
Precedence is constructor arguments, then the environment, then `.env`, then the
defaults below.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretNotConfigured(RuntimeError):
    """Raised when a JWT is needed and no signing secret was configured."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INKSPIRE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///var/data_dev.db"

    # No default, and checked at use time rather than declared required: a committed
    # default would sign real tokens on any machine that forgot to set one.
    jwt_secret: str | None = None
    jwt_ttl: int = 3600
    refresh_token_ttl: int = 604800

    # Cost factor for new password hashes. Stored hashes carry their own cost, so
    # changing this affects only passwords set from here on.
    bcrypt_rounds: int = 13

    # Login throttling per client address. Zero attempts disables it.
    login_max_attempts: int = 5
    login_interval: int = 60

    cors_allow_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$"

    def jwt_secret_or_raise(self) -> str:
        if not self.jwt_secret:
            raise SecretNotConfigured(
                "No JWT signing secret configured. Set INKSPIRE_JWT_SECRET in the "
                "environment or in .env.local. Generate one with: "
                "python -c 'import secrets; print(secrets.token_hex(32))'"
            )
        return self.jwt_secret


@lru_cache
def get_settings() -> Settings:
    """The process-wide settings. Cached, so `.env` is read once."""
    return Settings()
