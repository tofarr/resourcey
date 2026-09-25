"""Shared fixtures for the v2 message-board test suite.

The message-board app is built on ``v2``, whose two env-driven edges read the
process-wide ``APP`` prefix:

* SQL connections — ``APP_SQL_CONNECTIONS_0_NAME`` / ``_URL``;
* cursor encryption — ``APP_ENCRYPTION_KEY_ID`` / ``_VALUE`` (v2 degrades to a
  loud dev default when unset, but a test wants a stable key).

``v2`` does no ``.env`` loading, so the suite sets these directly. The autouse
fixture also clears the per-class config caches and the process-wide session
manager / encryption service, so every test rebuilds against its own env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from resourcey.v2.config.config_base import _reset_config_prefix
from resourcey.v2.encryption.encryption_service import clear_encryption_service_cache
from resourcey.v2.sql.session_manager import clear_sql_session_manager_cache


@pytest.fixture(autouse=True)
def _v2_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throwaway SQLite connection and cursor key around every test."""
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-cursors")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    """Drop the config / session-manager / encryption caches for a fresh build."""
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()
