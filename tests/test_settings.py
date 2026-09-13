# -*- coding: utf-8 -*-
"""Where configuration comes from, and which source wins.

The order matters in practice: `.env` is committed and holds defaults, `.env.local` is
not committed and holds the signing secret, and the environment overrides both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkspire_api.settings import SecretNotConfigured, Settings


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch) -> Path:
    """A working directory with no configuration files, since the paths are relative."""
    monkeypatch.chdir(tmp_path)
    for name in ("INKSPIRE_JWT_SECRET", "INKSPIRE_JWT_TTL", "INKSPIRE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_defaults_apply_with_no_configuration_at_all(env_dir: Path) -> None:
    settings = Settings()
    assert settings.jwt_ttl == 3600
    assert settings.jwt_secret is None


def test_dot_env_is_read(env_dir: Path) -> None:
    (env_dir / ".env").write_text("INKSPIRE_JWT_TTL=60\n", encoding="utf-8")
    assert Settings().jwt_ttl == 60


def test_dot_env_local_overrides_dot_env(env_dir: Path) -> None:
    """The committed file holds defaults; the uncommitted one holds what differs."""
    (env_dir / ".env").write_text("INKSPIRE_JWT_TTL=60\n", encoding="utf-8")
    (env_dir / ".env.local").write_text("INKSPIRE_JWT_TTL=120\n", encoding="utf-8")
    assert Settings().jwt_ttl == 120


def test_a_secret_in_dot_env_local_is_found(env_dir: Path) -> None:
    """Where the README and the error message both say to put it."""
    (env_dir / ".env.local").write_text("INKSPIRE_JWT_SECRET=" + "s" * 32, encoding="utf-8")
    assert Settings().jwt_secret_or_raise() == "s" * 32


def test_the_environment_beats_both_files(env_dir: Path, monkeypatch) -> None:
    (env_dir / ".env").write_text("INKSPIRE_JWT_TTL=60\n", encoding="utf-8")
    (env_dir / ".env.local").write_text("INKSPIRE_JWT_TTL=120\n", encoding="utf-8")
    monkeypatch.setenv("INKSPIRE_JWT_TTL", "180")
    assert Settings().jwt_ttl == 180


def test_arguments_beat_the_environment(env_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("INKSPIRE_JWT_TTL", "180")
    assert Settings(jwt_ttl=240).jwt_ttl == 240


def test_unprefixed_keys_are_ignored(env_dir: Path) -> None:
    """The same file configures another application, whose keys carry no prefix."""
    (env_dir / ".env").write_text("APP_ENV=dev\nDATABASE_URL=sqlite:///elsewhere.db\n", encoding="utf-8")
    assert Settings().database_url == "sqlite:///var/data_dev.db"


def test_a_missing_secret_names_where_to_put_one(env_dir: Path) -> None:
    with pytest.raises(SecretNotConfigured, match=".env.local"):
        Settings().jwt_secret_or_raise()
