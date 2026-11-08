"""Encrypt / decrypt the secret fields of a record at the storage boundary (issue #202).

Both storage backends call these with a record keyed by DTO field name, so the
at-rest behaviour (what is encrypted, how ``None`` and legacy values are
treated) is defined once.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from pydantic import SecretStr

from resourcey.encryption.encryption_service import EncryptionService


def unwrap_secret(value: Any) -> Any:
    """A :class:`~pydantic.SecretStr` as its plaintext; anything else unchanged."""
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def encrypt_fields(
    encryption: EncryptionService, record: dict[str, Any], names: Collection[str]
) -> dict[str, Any]:
    """A copy of ``record`` with each ``names`` field encrypted (``None`` stays ``None``)."""
    result = dict(record)
    for name in names:
        value = unwrap_secret(result.get(name))
        if value is not None:
            result[name] = encryption.encrypt_value(str(value))
    return result


def decrypt_fields(
    encryption: EncryptionService, record: dict[str, Any], names: Collection[str]
) -> dict[str, Any]:
    """A copy of ``record`` with each ``names`` field decrypted.

    A value that is not ciphertext is a row written before encryption was
    enabled; it passes through unchanged and is encrypted the next time the row
    is written. Ciphertext that fails authentication or names an unknown key
    raises, so a wrong or rotated-away key is never read as plaintext.
    """
    result = dict(record)
    for name in names:
        value = unwrap_secret(result.get(name))
        if isinstance(value, str) and encryption.is_ciphertext(value):
            result[name] = encryption.decrypt_value(value)
    return result
