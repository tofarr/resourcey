"""Root conftest: make the EncryptionService available to every test.

Cursor pagination (issue #35) encrypts cursors via the process-wide
``EncryptionService``, whose key is read from ``RESOURCEY_ENCRYPTION_*`` env
vars. Those vars are required (no plaintext fallback), so tests that exercise
``search`` need a throwaway key set before the singleton is first built. This
autouse fixture sets a default key around every test and clears the singleton
cache so each test rebuilds against the current env (tests that set their own
encryption env via ``monkeypatch`` override these defaults for that test).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from resourcey.encryption.encryption_service import clear_encryption_service_cache


@pytest.fixture(autouse=True)
def _encryption_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "test-secret-key-for-cursors")
    monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_ID", raising=False)
    monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", raising=False)
    clear_encryption_service_cache()
    yield
    clear_encryption_service_cache()
