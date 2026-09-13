# -*- coding: utf-8 -*-
"""Configuration, read from the environment and from `.env`.

Every key is prefixed `INKSPIRE_`; unprefixed keys in the file are ignored.
Precedence is constructor arguments, then the environment, then `.env`, then the
defaults below.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretNotConfigured(RuntimeError):
    """Raised when a JWT is needed and no signing secret was configured."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INKSPIRE_",
        # Layered, last one winning: `.env` is committed and holds defaults, `.env.local`
        # is not committed and holds secrets and per-machine paths.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///var/data_dev.db"

    #: The story repository: a git working tree holding `stories/<slug>/`. It is not
    #: inside this project, so the path is per-machine and belongs in `.env.local`.
    data_root: Path = Path("var/novel-data")

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

    #: Maps a provider name to its base URL and optional API key. Holds API keys, so
    #: it is not committed. Absent on an installation with no provider configured, in
    #: which case no model is offered.
    llm_providers_file: Path = Path("config/providers.yaml")

    llm_temperature: float = 1.0

    # Context window, sent to Ollama providers only, and only when set. A reasoning
    # model can spend a small window entirely on thinking and never answer.
    llm_num_ctx: int | None = None

    # The longest acceptable gap between two streamed chunks. A whole generation may
    # take much longer than this; only silence ends it.
    llm_timeout: float = 120.0

    # Ollama's `think` extension: True asks a reasoning model to think, False asks it
    # not to. Left unset the key is not sent at all, which is what a provider speaking
    # only the chat-completions specification expects.
    llm_think: bool | None = None

    llm_cache_ttl: int = 3600

    # Generation requests per authenticated account. Zero disables the limit.
    llm_limit: int = 20
    llm_interval: int = 60

    @field_validator("data_root", "llm_providers_file")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        """`~` in a path is expanded, since these are paths a person types."""
        return value.expanduser()

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
