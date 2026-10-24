"""Context-driven serialization for ``SecretStr`` / secret-bearing fields (issue #118).

An optional **context object** is passed to ``model_dump(..., context=...)`` /
``model_validate(..., context=...)``. If it carries an ``encryption_service``,
secret values are encrypted on dump and decrypted on load; otherwise
``expose_secrets: true`` dumps them in plaintext; otherwise they are redacted.

The helpers here are wired onto generated models via ``field_serializer`` /
``field_validator`` (see :mod:`resourcey.core.dto`).

This module is part of the bottom ``util`` layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

if TYPE_CHECKING:
    from resourcey.encryption.encryption_service import EncryptionService

# Sentinel used to detect a missing/empty context regardless of how the field
# serializer is invoked (``None`` is possible in FastAPI / pydantic configs).
_EMPTY: dict[str, Any] = {}


def _ctx_dict(info: Any | None) -> dict[str, Any]:
    """Extract the dict view of a pydantic context (``info.context``)."""
    if info is None:
        return _EMPTY
    ctx = getattr(info, "context", None)
    if ctx is None:
        return _EMPTY
    if isinstance(ctx, dict):
        return ctx
    return dict(ctx)


def encryption_service_from_context(info: Any | None) -> EncryptionService | None:
    """The ``encryption_service`` from the serialization context, or ``None``.

    Duck-typed (an ``encrypt_value`` attribute) so this module has no
    import-time dependency on the encryption package.
    """
    enc = _ctx_dict(info).get("encryption_service")
    return enc if enc is not None and hasattr(enc, "encrypt_value") else None


def expose_secrets_from_context(info: Any | None) -> bool:
    """Whether the context requests plaintext secrets (``expose_secrets``)."""
    return bool(_ctx_dict(info).get("expose_secrets"))


def dump_secret_str(secret: SecretStr, info: Any | None = None) -> str:
    """Serialize a :class:`SecretStr`: encrypt, else expose, else redact.

    An explicit encryption request wins over a stray ``expose_secrets`` flag, so
    it can never leak plaintext.
    """
    enc = encryption_service_from_context(info)
    if enc is not None:
        return enc.encrypt_value(secret.get_secret_value())
    if expose_secrets_from_context(info):
        return secret.get_secret_value()
    return str(secret)


def load_secret_str(secret: SecretStr | str, info: Any | None = None) -> str:
    """Deserialize a stored secret, decrypting JWE ciphertext when a service is in context."""
    enc = encryption_service_from_context(info)
    raw = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
    if enc is not None:
        return enc.decrypt_value(raw)
    return raw
