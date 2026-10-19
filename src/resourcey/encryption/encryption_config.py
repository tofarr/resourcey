"""Encryption key configuration (issue #78).

``EncryptionKeysConfig`` parses the process-wide prefix: the single
``encryption_key`` (used for all new encryption) and a ``decryption_keys`` list
(every key that may decrypt existing data, enabling rotation). An absent key
degrades to a loud dev default rather than a build failure; a *partially*
specified one is a hard error.

This module imports no code outside the framework.
"""

from __future__ import annotations

import logging
from typing import Any, Self

from pydantic import BaseModel, Field, SecretStr, field_serializer, model_validator

from resourcey.config.config_base import BaseConfig

logger = logging.getLogger(__name__)


def _default_encryption_key() -> EncryptionKeyConfig:
    """The dev default key, with the warning that goes with it.

    A factory (not a shared default) so the warning fires per construction and
    the prefix is read at instance initialization.
    """
    logger.warning(
        "⚠️ Using Default Encryption Key. Set %s_ENCRYPTION_KEY_VALUE in a non dev environment.",
        BaseConfig.get_prefix(),
    )
    return EncryptionKeyConfig(value=SecretStr("changeme"))


class EncryptionKeyConfig(BaseModel):
    """Configuration for a single encryption / decryption key.

    ``id`` is carried in the JWE ``kid`` header so the correct decryption key can
    be selected on read. ``value`` serializes as redacted unless an
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

    The encryption key is auto-included in ``decryption_keys`` when missing, so
    rotation works without breaking previously encrypted values.
    """

    encryption_key: EncryptionKeyConfig = Field(default_factory=_default_encryption_key)
    decryption_keys: list[EncryptionKeyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_encryption_key_in_decryption_keys(self) -> Self:
        enc_key_id = self.encryption_key.id
        if not any(k.id == enc_key_id for k in self.decryption_keys):
            self.decryption_keys = [self.encryption_key, *self.decryption_keys]
        return self
