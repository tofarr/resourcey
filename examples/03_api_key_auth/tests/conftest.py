"""Shared fixtures for the API-key-auth example suite.

Cursor pagination encrypts search cursors via the process-wide
``EncryptionService``, whose key is read from ``RESOURCEY_ENCRYPTION_*`` env
vars (required, no plaintext fallback). This autouse fixture sets a throwaway
key around every test and clears the singleton cache so each test rebuilds
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
    clear_encryption_service_cache()
    yield
    clear_encryption_service_cache()
