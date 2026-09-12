# -*- coding: utf-8 -*-
"""The migrations and the models must describe the same schema.

The suite builds its database with `create_all`, so a missing migration would not
show up anywhere else: the tests would pass and `alembic upgrade head` would
produce a different database from the one they ran against.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from inkspire_api.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def alembic_config(tmp_path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config, url


def test_upgrade_head_creates_the_two_tables(alembic_config) -> None:
    config, url = alembic_config
    command.upgrade(config, "head")

    engine = create_engine(url)
    assert {"user", "refresh_token"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_the_migrations_match_the_models(alembic_config) -> None:
    config, url = alembic_config
    command.upgrade(config, "head")

    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert differences == [], "models and migrations disagree; run alembic revision --autogenerate"


def test_downgrade_removes_them_again(alembic_config) -> None:
    config, url = alembic_config
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(url)
    assert not {"user", "refresh_token"} & set(inspect(engine).get_table_names())
    engine.dispose()
