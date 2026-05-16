from __future__ import annotations

import asyncio
import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return settings.normalized_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    url = _url()

    # Redact URL for logging
    redacted_url = url
    if "@" in url:
        prefix, suffix = url.split("@", 1)
        if "//" in prefix:
            scheme, _auth = prefix.split("//", 1)
            redacted_url = f"{scheme}//****@{suffix}"

    logger = logging.getLogger("alembic.runtime.migration")
    logger.info("Connecting to database: %s", redacted_url)

    section["sqlalchemy.url"] = url
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    except Exception as e:
        logger.error("Failed to connect to database at %s: %s", redacted_url, e)
        # If we are in Docker and using localhost, provide a hint.
        if "localhost" in url and os.path.exists("/.dockerenv"):
            logger.warning(
                "HINT: You are running in Docker but DATABASE_URL points to 'localhost'. "
                "Use the service name (e.g., 'db') instead."
            )
        raise
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
