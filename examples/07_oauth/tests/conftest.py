"""Shared fixtures for the OAuth example suite.

The app is built on the framework, whose env-driven edges read the process-wide
``APP`` prefix:

* SQL connections — ``APP_SQL_CONNECTIONS_0_NAME`` / ``_URL``;
* cursor / flow-state / session-cookie encryption — ``APP_ENCRYPTION_KEY_ID`` /
  ``_VALUE`` (the framework degrades to a loud dev default when unset, but a test
  wants a stable key).

The OAuth clients are an env edge too (``APP_OAUTH_CLIENTS_<n>_*``); the tests
pass an ``IdpConfig`` explicitly instead, so they do not depend on the process
environment. The framework does no ``.env`` loading, so the suite sets these
directly, and the autouse fixture clears the per-class config caches and the
process-wide session manager / encryption service, so every test rebuilds against
its own env.
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
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-07")
    # The committed .env disables the HTTPS-only flag for the local demo; the
    # tests run over http://test, so the session cookie must be sendable.
    monkeypatch.setenv("APP_SESSION_COOKIE_SECURE", "false")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    """Drop the config / session-manager / encryption caches for a fresh build."""
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    clear_encryption_service_cache()
