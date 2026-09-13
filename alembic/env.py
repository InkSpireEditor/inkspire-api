# -*- coding: utf-8 -*-
"""Alembic environment.

The database URL comes from the application settings rather than from alembic.ini,
so `alembic upgrade head` and the running API always address the same database. Set
INKSPIRE_DATABASE_URL to migrate another one.

`render_as_batch` is on because SQLite cannot ALTER most things: Alembic rewrites
those changes as copy-table-and-swap.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from inkspire_api.models import Base
from inkspire_api.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
