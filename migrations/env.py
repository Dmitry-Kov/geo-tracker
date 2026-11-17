# -*- coding: utf-8 -*-
"""Alembic environment. The service (migrate_database) hands over its own
connection; the alembic CLI falls back to sqlalchemy.url from alembic.ini
or, when that is empty, the service's default database. render_as_batch is
on so future ALTERs work on SQLite."""
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


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)  # the caller owns the connection and its transaction
        return
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url) if url else make_engine()
    with connectable.connect() as connection:
        _run(connection)
        connection.commit()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
