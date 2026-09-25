"""Encryption key configuration (issue #78, migrated from ``resourcey.encryption``).

``EncryptionKeyConfig`` describes a single symmetric key used for at-rest
field encryption. A root config block holds the single ``encryption_key``
(used for all new encryption) and a ``decryption_keys`` list (every key that
may decrypt existing data, enabling rotation). A ``model_validator``
guarantees the encryption key is present in ``decryption_keys``.

``EncryptionKeysConfig`` is a :class:`~resourcey.v2.config.config_base.BaseConfig`
(issue #111), so it parses the process-wide prefix (``APP`` by default):
``APP_ENCRYPTION_KEY_ID`` / ``APP_ENCRYPTION_KEY_VALUE`` and
``APP_DECRYPTION_KEYS_<n>_ID`` / ``_VALUE``. The ``encryption_key`` is no
longer required: an absent key degrades to a loud dev default
(``EncryptionKeyConfig(value="changeme")`` plus a warning) instead of a build
failure. ``EncryptionKeyConfig`` stays a plain ``BaseModel`` nested inside the
block, mirroring ``DbConfig`` under ``SqlConfig``. An ``EncryptionService`` is
otherwise constructed from an injected instance, so how a caller obtains the
config (env, secrets, a config file) remains the caller's concern.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``
(only Pydantic, the standard library, and ``v2``'s own config base).
"""

from __future__ import annotations

import logging
from typing import Any, Self

from pydantic import BaseModel, Field, SecretStr, field_serializer, model_validator

from resourcey.v2.config.config_base import BaseConfig

logger = logging.getLogger(__name__)


def _default_encryption_key() -> EncryptionKeyConfig:
    """The dev default key, with the warning that goes with it.

    A factory (not a shared default) so the warning fires per construction and
    ``BaseConfig.get_prefix()`` is read lazily at instance initialization, not
    at class-definition time.
    """
    logger.warning(
        "⚠️ Using Default Encryption Key. Set %s_ENCRYPTION_KEY_VALUE in a non dev environment.",
        BaseConfig.get_prefix(),
    )
    return EncryptionKeyConfig(value=SecretStr("changeme"))


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


class EncryptionKeysConfig(BaseConfig):
    """Root config block for the encryption keys.

    ``encryption_key`` is the single key used for all new encryption. It
    defaults to a loud dev value when neither its id nor value is configured;
    a *partially* specified key (an id with no value, or vice versa) is a hard
    error, since that is a misconfiguration rather than "unset".
    ``decryption_keys`` lists every key that may decrypt existing data; the
    encryption key is auto-included when missing, so rotation works without
    breaking previously encrypted values.
    """

    encryption_key: EncryptionKeyConfig = Field(default_factory=_default_encryption_key)
    decryption_keys: list[EncryptionKeyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_encryption_key_in_decryption_keys(self) -> Self:
        enc_key_id = self.encryption_key.id
        if not any(k.id == enc_key_id for k in self.decryption_keys):
            self.decryption_keys = [self.encryption_key, *self.decryption_keys]
        return self
