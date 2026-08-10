"""Alembic environment.

Runs the migrations through the same asyncpg driver the service uses, so no
synchronous database driver has to be installed. The database URL always comes
from the application settings, never from ``alembic.ini``, so migrations and
the service can never point at different databases.
"""

from __future__ import annotations

import asyncio
import logging.config

import alembic
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config as app_config
from app.db import models

alembic_config = alembic.context.config
if alembic_config.config_file_name is not None:
    logging.config.fileConfig(alembic_config.config_file_name)

alembic_config.set_main_option(
    "sqlalchemy.url", app_config.get_settings().database_url
)
target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    """Emits the migration SQL without connecting to a database."""
    alembic.context.configure(
        url=alembic_config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with alembic.context.begin_transaction():
        alembic.context.run_migrations()


def _do_run_migrations(connection: sqlalchemy.Connection) -> None:
    """Runs the migrations on an already opened synchronous connection."""
    alembic.context.configure(
        connection=connection, target_metadata=target_metadata
    )
    with alembic.context.begin_transaction():
        alembic.context.run_migrations()


async def run_migrations_online() -> None:
    """Connects with asyncpg and applies the pending migrations."""
    connectable = sa_asyncio.async_engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=sqlalchemy.pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if alembic.context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
