"""Alembic environment for the v2 message-board example.

Unlike the v1 examples, this ``env.py`` does not import a resource manifest: in
v2 the ORM models *are* the schema of record, so autogeneration diffs against
``message_board.models.Base.metadata`` directly. Importing the models module is
enough to register every table on that metadata.

The database URL comes from the same ``APP_SQL_CONNECTIONS_0_URL`` the app reads
(so the migration and the running app cannot disagree), converted to its
*synchronous* counterpart because Alembic drives a sync engine.

Alembic needs the environment loaded (v2 does no .env loading): run it with
``uv run --env-file .env alembic ...`` or export ``APP_SQL_CONNECTIONS_0_URL``.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from message_board.models import Base

config = context.config

# The ORM models are the schema of record; their metadata is the diff target.
target_metadata = Base.metadata


def _database_url() -> str:
    """The sync SQLAlchemy URL for the example's configured connection."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    url = os.environ.get("APP_SQL_CONNECTIONS_0_URL")
    if not url:
        raise RuntimeError(
            "Set APP_SQL_CONNECTIONS_0_URL (or sqlalchemy.url in alembic.ini) "
            "before running Alembic; v2 does no .env loading. Try "
            "`uv run --env-file .env alembic ...`."
        )
    return _sync_database_url(url)


def _sync_database_url(url: str) -> str:
    """Convert an async SQLAlchemy URL to its sync counterpart for Alembic."""
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+psycopg2")
    if "+aiosqlite" in url:
        return url.replace("+aiosqlite", "")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect and apply)."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
