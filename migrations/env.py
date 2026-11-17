# -*- coding: utf-8 -*-
"""Alembic environment. The URL comes from the service (migrate_database)
or falls back to the service default DB for CLI use. render_as_batch is on
so future ALTERs work on SQLite."""
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import Base, make_engine  # noqa: E402

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: this file also runs inside the service
    # at startup. fileConfig's default disables every logger that already
    # exists — uvicorn's included — and the server would then run without a
    # startup line, access log or error tracebacks.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or str(make_engine().url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
