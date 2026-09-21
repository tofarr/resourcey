"""Shared fixtures for the users-and-permissions example E2E suite.

The secured services resolve the principal from a session cookie minted by the
dev IdP. Cursor pagination encrypts cursors and the dev IdP mints JWE tokens
via the process-wide ``EncryptionService``, whose keys are read from
``RESOURCEY_ENCRYPTION_*`` env vars. This autouse fixture sets throwaway keys
around every test and clears the singleton cache so each test rebuilds
against the env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from resourcey.encryption.encryption_service import clear_encryption_service_cache


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-03")
    monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_ID", raising=False)
    monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", raising=False)
    monkeypatch.setenv("RESOURCEY_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("RESOURCEY_AUTH_COOKIE_SAMESITE", "lax")
    monkeypatch.setenv("RESOURCEY_BASE_URL", "http://localhost:8083")
    clear_encryption_service_cache()
    yield
    clear_encryption_service_cache()
