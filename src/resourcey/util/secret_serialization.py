"""Context-driven serialization for ``SecretStr`` / secret-bearing fields.

Sensitive fields are serialized through a uniform pydantic-context convention
applied to every :class:`SecretStr` field:

* An optional **context object** is passed when serializing / deserializing
  (``model_dump(..., context=...)`` / ``model_validate(..., context=...)``).
* If the context carries an ``encryption_service`` (an
  :class:`~resourcey.encryption.encryption_service.EncryptionService`),
  :class:`SecretStr` values are encrypted on dump via
  :meth:`EncryptionService.encrypt_value` and decrypted on load via
  :meth:`EncryptionService.decrypt_value` (the column stores JWE ciphertext).
* Otherwise, if the context carries ``expose_secrets: true``, secrets are
  dumped in plaintext.
* Otherwise (no context / neither flag), secrets are **redacted**
  (``str(SecretStr)`` -> ``**********``).

The helpers here are wired onto generated models via ``field_serializer`` /
``field_validator`` (see :mod:`resourcey.resource.base`); they take the
pydantic ``SerializationInfo`` / ``ValidationInfo`` when available so the
context flows through. Ported from ohev2; the vendored utilities stay
self-contained (no external SDK dependency).
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

    Duck-typed (an ``encrypt_value`` attribute) so the util module has no
    import-time dependency on the encryption package, keeping the vendored
    utilities self-contained.
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


def dump_secret_map(
    values: dict[str, SecretStr] | None,
    info: Any | None = None,
) -> dict[str, str] | None:
    """Serialize a map of :class:`SecretStr` values per the convention above."""
    if values is None:
        return None
    return {key: dump_secret_str(item, info) for key, item in values.items()}


def load_secret_map(
    values: dict[str, str] | None,
    info: Any | None = None,
) -> dict[str, SecretStr] | None:
    """Deserialize a stored (ciphertext) map back to in-memory secrets."""
    if values is None:
        return None
    return {key: SecretStr(load_secret_str(item, info)) for key, item in values.items()}


def encrypt_secret_map(
    enc: EncryptionService | None,
    values: dict[str, SecretStr] | None,
) -> dict[str, str]:
    """Encrypt a map of secrets to JWE ciphertext for storage (service layer).

    With ``enc`` ``None`` (no encryption configured) the plaintext is stored
    as-is -- the caller must already have accepted that trade-off. In resourcey
    the encryption key is always configured, so the service layer always passes
    a live service.
    """
    if values is None:
        return {}
    if enc is None:
        return {key: item.get_secret_value() for key, item in values.items()}
    return {key: enc.encrypt_value(item.get_secret_value()) for key, item in values.items()}


def decrypt_secret_map(
    enc: EncryptionService | None,
    values: dict[str, str] | None,
) -> dict[str, SecretStr]:
    """Decrypt a stored (ciphertext) map back to in-memory :class:`SecretStr` values."""
    if values is None:
        return {}
    if enc is None:
        return {key: SecretStr(str(item)) for key, item in values.items()}
    return {key: SecretStr(enc.decrypt_value(str(item))) for key, item in values.items()}
