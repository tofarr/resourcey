"""Shared fixtures for the v2 MongoDB message-board test suite.

The app is built on ``v2``, whose env-driven edges read the process-wide ``APP``
prefix:

* Mongo connections — ``APP_MONGO_CONNECTIONS_0_NAME`` / ``_URL``;
* cursor encryption — ``APP_ENCRYPTION_KEY_ID`` / ``_VALUE`` (v2 degrades to a
  loud dev default when unset, but a test wants a stable key).

``v2`` does no ``.env`` loading, so the suite sets these directly. The autouse
fixture also clears the per-class config caches and the process-wide Mongo client
manager / encryption service, so every test rebuilds against its own env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from resourcey.v2.config.config_base import _reset_config_prefix
from resourcey.v2.encryption.encryption_service import clear_encryption_service_cache
from resourcey.v2.mongo.mongo_client import clear_mongo_client_manager_cache


@pytest.fixture(autouse=True)
def _v2_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An embedded Mongo connection and a throwaway cursor key around every test."""
    monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_URL", "embedded://message_board_test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-cursors")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    """Drop the config / client-manager / encryption caches for a fresh build."""
    _reset_config_prefix()
    clear_mongo_client_manager_cache()
    clear_encryption_service_cache()
