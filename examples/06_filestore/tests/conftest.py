"""Shared fixtures for the file-store example suite.

The app is built on the framework, whose env-driven edges read the process-wide
``APP`` prefix:

* SQL connections — ``APP_SQL_CONNECTIONS_0_NAME`` / ``_URL``;
* encryption — ``APP_ENCRYPTION_KEY_ID`` / ``_VALUE`` (used for pagination
  cursors *and* the framework-signed capability URLs the local medium mints).

The framework does no ``.env`` loading, so the suite sets these directly. The
autouse fixture also clears the per-class config caches and the process-wide
session manager / encryption service, so every test rebuilds against its own env.
The byte medium itself is injected per test (a :class:`LocalFileStore` rooted in
``tmp_path``), so no test touches the real ``./.resourcey_files`` directory.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from resourcey.config.config_base import _reset_config_prefix
from resourcey.encryption.encryption_service import clear_encryption_service_cache
from resourcey.sql.session_manager import clear_sql_session_manager_cache


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throwaway SQLite connection and encryption key around every test."""
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-06")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    """Drop the config / session-manager / encryption caches for a fresh build."""
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()
