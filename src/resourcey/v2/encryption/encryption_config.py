"""Encryption key configuration (issue #78, migrated from ``resourcey.encryption``).

``EncryptionKeyConfig`` describes a single symmetric key used for at-rest
field encryption. A root config block holds the single ``encryption_key``
(used for all new encryption) and a ``decryption_keys`` list (every key that
may decrypt existing data, enabling rotation). A ``model_validator``
guarantees the encryption key is present in ``decryption_keys``.

The ``encryption_key`` is **required** (no default): there is no
plaintext-storage fallback. This ``v2`` copy is *config-free* — an
``EncryptionService`` is constructed from an injected instance, so how a
caller obtains the config (env, secrets, a config file) is the caller's
concern.

This module is part of ``v2/``: it imports no ``resourcey`` code (only
Pydantic and the standard library).
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, Field, SecretStr, field_serializer, model_validator


class EncryptionKeyConfig(BaseModel):
    """Configuration for a single encryption / decryption key.

    Attributes:
        id: Key identifier carried in the JWE ``kid`` header so the correct
            decryption key can be selected on read. Defaults to ``"default"``.
        value: The symmetric secret. Serializes as redacted unless an
            ``expose_secrets`` serialization context is set.
    """

    id: str = "default"
    value: SecretStr

    @field_serializer("value")
    def _serialize_value(self, value: SecretStr, info: Any) -> str:
        if info.context and info.context.get("expose_secrets"):
            return value.get_secret_value()
        return str(value)


class EncryptionKeysConfig(BaseModel):
    """Root config block for the encryption keys.

    ``encryption_key`` is the single key used for all new encryption
    (required). ``decryption_keys`` lists every key that may decrypt existing
    data; the encryption key is auto-included when missing, so rotation works
    without breaking previously encrypted values.
    """

    encryption_key: EncryptionKeyConfig
    decryption_keys: list[EncryptionKeyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_encryption_key_in_decryption_keys(self) -> Self:
        enc_key_id = self.encryption_key.id
        if not any(k.id == enc_key_id for k in self.decryption_keys):
            self.decryption_keys = [self.encryption_key, *self.decryption_keys]
        return self
