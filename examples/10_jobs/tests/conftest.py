"""Shared fixtures for the durable-jobs example suite.

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory, created by applying the committed Alembic migration (the same revision
``alembic upgrade head`` applies), so the migration itself is verified. The app
is assembled through the real config path and driven over httpx's ASGI transport.

The runner is a real :class:`~resourcey.jobs.jobs_runner.JobRunner` entered with
the manifest, so a job enqueued through REST is claimed and run for real. The
sweep interval is short (0.05s) so a test need only await a moment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from jobs_example.app import build_app
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.config.config_base import _reset_config_prefix
from resourcey.encryption.encryption_service import clear_encryption_service_cache
from resourcey.jobs.jobs_config import JobsConfig
from resourcey.jobs.jobs_runner import JobRunner
from resourcey.sql.session_manager import SqlSessionManager, clear_sql_session_manager_cache
from resourcey.sql.sql_config import SqlConfig
from resourcey.tasks.task import BackgroundTasksConfig

ADMIN_KEY = "admin-key"
USER_KEY = "user-key"
# The principal the USER key acts as — the ``creator_id`` stamped on its jobs.
USER_ID = "11111111-1111-1111-1111-111111111111"


def _keys() -> ApiKeysConfig:
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="admin", key=SecretStr(ADMIN_KEY), roles=["ADMIN"]),
            ApiKeyConfig(
                id="user",
                key=SecretStr(USER_KEY),
                principal_id=USER_ID,
                roles=["USER"],
            ),
        ]
    )


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _sync_url(async_url: str) -> str:
    """Alembic drives a sync engine; mirror ``migrations/env.py``'s conversion."""
    return async_url.replace("+aiosqlite", "")


def _apply_migration(async_url: str) -> None:
    config = AlembicConfig()
    config.set_main_option("script_location", str(_migrations_dir()))
    config.set_main_option("sqlalchemy.url", _sync_url(async_url))
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Drop the config / session-manager / encryption caches around every test."""
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()
    yield
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()


@pytest_asyncio.fixture
async def wired(tmp_path: Path, monkeypatch) -> AsyncIterator[tuple]:
    """A fully wired client + manager + app + runner over a migrated SQLite file.

    Yields ``(client, session_manager, app, runner)``. The runner sweeps quickly
    (0.05s) and runs bodies concurrently; the task scheduler is left off so only
    the tests enqueue.
    """
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-10")
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    jobs_config = JobsConfig(
        jobs_sweep_interval_seconds=0.05,
        jobs_default_max_attempts=1,
    )
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app, runner = build_app(
        session_manager=manager,
        keys=_keys(),
        jobs_config=jobs_config,
        tasks_config=BackgroundTasksConfig(background_tasks=[]),
        runner_enabled=True,
        scheduler_enabled=False,
    )
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, manager, app, runner
    finally:
        await manifest.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(wired: tuple) -> AsyncClient:
    """Just the REST client (the common case)."""
    return wired[0]


@pytest_asyncio.fixture
async def runner(wired: tuple) -> JobRunner:
    """Just the live job runner."""
    return wired[3]


def _h(key: str) -> dict[str, str]:
    from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME

    return {API_KEY_HEADER_NAME: key}
