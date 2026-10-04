"""Shared fixtures for the realtime-example test suite.

The realtime-example app is built on the framework, whose env-driven edges
read the process-wide ``APP`` prefix:

* SQL connections — ``APP_SQL_CONNECTIONS_0_NAME`` / ``_URL``;
* cursor encryption — ``APP_ENCRYPTION_KEY_ID`` / ``_VALUE`` (the framework
  degrades to a loud dev default when unset, but a test wants a stable key).

``RealtimeConfig``'s ``channel`` is a ``CHANNEL_CLASS``-selected ``LazyField``
(unprefixed, like ``FileStoreConfig``'s ``MEDIUM_CLASS``) — most tests build
their own channel directly via ``build_app(channel=...)`` and never touch it.
``RealtimeConfig`` is a ``BaseConfig`` too, so its cache is cleared by the same
``_reset_config_prefix()`` call below.

The framework does no ``.env`` loading, so the suite sets these directly. The
autouse fixture also clears the per-class config caches and the process-wide
session manager / encryption service, so every test rebuilds against its own
env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from resourcey.config.config_base import _reset_config_prefix
from resourcey.encryption.encryption_service import clear_encryption_service_cache
from resourcey.sql.session_manager import clear_sql_session_manager_cache


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throwaway SQLite connection and cursor key around every test."""
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-cursors")
    monkeypatch.delenv("CHANNEL_CLASS", raising=False)
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    """Drop the config / session-manager / encryption caches for a fresh build."""
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()
