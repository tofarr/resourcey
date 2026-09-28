"""Context-driven serialization for ``SecretStr`` / secret-bearing fields (issue #118).

Sensitive fields are serialized through a uniform pydantic-context convention
applied to every :class:`~pydantic.SecretStr` field:

* An optional **context object** is passed when serializing / deserializing
  (``model_dump(..., context=...)`` / ``model_validate(..., context=...)``).
* If the context carries an ``encryption_service``, :class:`SecretStr` values are
  encrypted on dump via ``encrypt_value`` and decrypted on load via
  ``decrypt_value`` (the column stores JWE ciphertext).
* Otherwise, if the context carries ``expose_secrets: true``, secrets are dumped
  in plaintext.
* Otherwise (no context / neither flag), secrets are **redacted**
  (``str(SecretStr)`` -> ``**********``).

The helpers here are wired onto generated models via ``field_serializer`` /
``field_validator`` (see :mod:`resourcey.v2.core.dto`); they take the pydantic
``SerializationInfo`` / ``ValidationInfo`` when available so the context flows
through.

This module is part of ``v2/util`` (the bottom layer): it imports no other
``resourcey`` module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

if TYPE_CHECKING:
    from resourcey.v2.encryption.encryption_service import EncryptionService

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

    Duck-typed (an ``encrypt_value`` attribute) so the util module has no
    import-time dependency on the encryption package, keeping the bottom layer
    self-contained.
    """
    enc = _ctx_dict(info).get("encryption_service")
    return enc if enc is not None and hasattr(enc, "encrypt_value") else None


def expose_secrets_from_context(info: Any | None) -> bool:
    """Whether the context requests plaintext secrets (``expose_secrets``)."""
    return bool(_ctx_dict(info).get("expose_secrets"))


def dump_secret_str(secret: SecretStr, info: Any | None = None) -> str:
    """Serialize a :class:`SecretStr` per the convention above.

    Encryption (context ``encryption_service``) wins over plaintext exposure
    (context ``expose_secrets``), which wins over redaction -- an explicit
    encryption request must never leak plaintext through a stray flag.
    """
    enc = encryption_service_from_context(info)
    if enc is not None:
        return enc.encrypt_value(secret.get_secret_value())
    if expose_secrets_from_context(info):
        return secret.get_secret_value()
    return str(secret)


def load_secret_str(secret: SecretStr | str, info: Any | None = None) -> str:
    """Deserialize a stored secret per the convention above.

    When the context carries an ``encryption_service`` the stored value is JWE
    ciphertext and is decrypted to plaintext. Otherwise the value is passed
    through unchanged.
    """
    enc = encryption_service_from_context(info)
    raw = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
    if enc is not None:
        return enc.decrypt_value(raw)
    return raw
