"""At-rest value encryption and auth-token JWE encryption (issue #7, #4).

``EncryptionService`` provides two JWE compact-serialization paths, both using
``alg=dir`` + ``enc=A256GCM`` (direct symmetric key, AES-256-GCM):

* **Value encryption** (:meth:`encrypt_value` / :meth:`decrypt_value`) —
  encrypts sensitive field values at rest. The plaintext is wrapped in a
  ``{"v": plaintext}`` JSON payload before encryption.
* **Auth-token encryption** (:meth:`create_jwe_token` / :meth:`decrypt_jwe_token`)
  — encrypts arbitrary claim dicts (with optional ``exp``) as JWE tokens used
  for session cookies, access tokens, refresh tokens, and authorization codes
  by the auth layer (issue #4).

The symmetric key is SHA-256 derived from the configured secret. The key
``id`` is carried in the JWE ``kid`` header so the correct decryption key can
be selected on read, enabling key rotation.

A JWE algorithm registry pins ``dir`` + ``A256GCM`` only (no cryptographic
agility) and caps ciphertext length. A process-wide cached accessor
(:func:`get_encryption_service`) returns the singleton built from config.

Ported from ohev2's ``encryption_service.py``; the vendored utilities stay
self-contained (no external SDK dependency).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from functools import lru_cache
from typing import Any

from joserfc import jwe
from joserfc.jwk import OctKey

from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.util import utc_now

# Only allow dir + A256GCM to prevent cryptographic agility attacks.
_JWE_REGISTRY = jwe.JWERegistry(algorithms=["dir", "A256GCM"])
_JWE_REGISTRY.max_ciphertext_length = 100 * 1024 * 1024  # 100MB


def _derive_symmetric_key(secret: str) -> OctKey:
    """Derive a 256-bit symmetric key from a secret string (SHA-256)."""
    key_256 = hashlib.sha256(secret.encode()).digest()
    return OctKey.import_key(key_256)


def _urlsafe_b64decode(data: str) -> bytes:
    """Decode a base64url string (without padding) to bytes."""
    return base64.urlsafe_b64decode(data)


def _jwe_kid(ciphertext: str) -> str:
    """Extract the ``kid`` from a JWE compact serialization's protected header.

    The protected header is the first dot-separated segment, base64url JSON.
    Parsing it directly lets the decryption key be selected before the
    authenticated decryption is attempted. Raises ``ValueError`` for malformed
    tokens or tokens lacking a ``kid``.
    """
    segments = ciphertext.split(".")
    if len(segments) != 5:
        raise ValueError("Invalid JWE token format")
    header_b64 = segments[0]
    try:
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        header: dict[str, Any] = json.loads(_urlsafe_b64decode(padded))
    except Exception as exc:
        raise ValueError("Invalid JWE token format") from exc
    key_id = header.get("kid")
    if not key_id:
        raise ValueError("Token does not contain 'kid' header with key ID")
    return str(key_id)


class EncryptionService:
    """Encrypt / decrypt sensitive field values via JWE compact serialization."""

    def __init__(self, config: EncryptionKeysConfig) -> None:
        self._encryption_key = config.encryption_key
        self._decryption_keys: dict[str, EncryptionKeyConfig] = {
            k.id: k for k in config.decryption_keys
        }

    @property
    def encryption_key_id(self) -> str:
        """The id of the key used for all new encryption."""
        return self._encryption_key.id

    @property
    def decryption_key_ids(self) -> list[str]:
        """All key ids that may decrypt existing data."""
        return list(self._decryption_keys.keys())

    def _get_decryption_key(self, key_id: str) -> EncryptionKeyConfig:
        if key_id not in self._decryption_keys:
            raise ValueError(f"Key ID '{key_id}' not found")
        return self._decryption_keys[key_id]

    def create_jwe_token(
        self,
        payload: dict[str, Any],
        expires_in: timedelta | None = None,
    ) -> str:
        """Encrypt a claim dict into a JWE compact token for auth use.

        Adds ``iat`` (issued-at) and, when *expires_in* is given, ``exp``
        (expiry) claims to the payload before encryption. The encryption key's
        ``id`` is carried in the ``kid`` header so the correct decryption key
        can be selected on read.
        """
        now = utc_now()
        jwt_payload: dict[str, Any] = {
            **payload,
            "iat": int(now.timestamp()),
        }
        if expires_in is not None:
            jwt_payload["exp"] = int((now + expires_in).timestamp())

        symmetric_key = _derive_symmetric_key(self._encryption_key.value.get_secret_value())
        protected_header = {
            "alg": "dir",
            "enc": "A256GCM",
            "kid": self._encryption_key.id,
        }
        return jwe.encrypt_compact(
            protected_header,
            json.dumps(jwt_payload).encode("utf-8"),
            symmetric_key,
            registry=_JWE_REGISTRY,
        )

    def decrypt_jwe_token(self, token: str) -> dict[str, Any]:
        """Decrypt a JWE compact token back to its claim dict.

        Selects the decryption key from the ``kid`` header; an unknown ``kid``
        raises ``ValueError``. Unlike :meth:`decrypt_value` (which unwraps a
        ``{"v": ...}`` payload), this returns the raw claim dict so the auth
        layer can read ``sub``, ``exp``, ``ttyp``, etc. directly.
        """
        key_id = _jwe_kid(token)
        key = self._get_decryption_key(key_id)
        symmetric_key = _derive_symmetric_key(key.value.get_secret_value())
        try:
            result = jwe.decrypt_compact(token, symmetric_key, registry=_JWE_REGISTRY)
        except Exception as exc:
            raise ValueError("Token decryption failed") from exc
        if result.plaintext is None:
            raise ValueError("Decryption produced no plaintext")
        payload: dict[str, Any] = json.loads(result.plaintext)
        return payload

    def encrypt_value(self, plaintext: str) -> str:
        """Encrypt a plaintext string into a JWE compact ciphertext.

        The encryption key's ``id`` is carried in the ``kid`` header so the
        correct decryption key can be selected on read.
        """
        symmetric_key = _derive_symmetric_key(self._encryption_key.value.get_secret_value())
        protected_header = {
            "alg": "dir",
            "enc": "A256GCM",
            "kid": self._encryption_key.id,
        }
        payload = json.dumps({"v": plaintext}).encode("utf-8")
        return jwe.encrypt_compact(
            protected_header,
            payload,
            symmetric_key,
            registry=_JWE_REGISTRY,
        )

    def decrypt_value(self, ciphertext: str) -> str:
        """Decrypt a JWE compact ciphertext back to the original plaintext.

        Selects the decryption key from the ``kid`` header; an unknown ``kid``
        raises ``ValueError``. The protected header is parsed from the first
        compact segment (base64url JSON) so the key can be selected before the
        authenticated decryption is attempted.
        """
        key_id = _jwe_kid(ciphertext)
        key = self._get_decryption_key(key_id)
        symmetric_key = _derive_symmetric_key(key.value.get_secret_value())
        try:
            result = jwe.decrypt_compact(ciphertext, symmetric_key, registry=_JWE_REGISTRY)
        except Exception as exc:
            raise ValueError("Token decryption failed") from exc
        if result.plaintext is None:
            raise ValueError("Decryption produced no plaintext")
        payload: dict[str, Any] = json.loads(result.plaintext)
        return str(payload["v"])


@lru_cache(maxsize=1)
def get_encryption_service() -> EncryptionService:
    """Return the process-wide singleton :class:`EncryptionService`.

    Built from :class:`EncryptionKeysConfig` loaded from environment variables
    via :func:`~resourcey.util.env_parser.from_env` under the ``RESOURCEY``
    prefix (e.g. ``RESOURCEY_ENCRYPTION_KEY_ID``,
    ``RESOURCEY_ENCRYPTION_KEY_VALUE``,
    ``RESOURCEY_DECRYPTION_KEYS_0_ID`` / ``_VALUE``, ...). The result is
    cached so repeated calls return the same instance. The encryption key is
    always required, so there is no plaintext-storage fallback.
    """
    return EncryptionService(_encryption_keys_from_env())


def _encryption_keys_from_env() -> EncryptionKeysConfig:
    """Build :class:`EncryptionKeysConfig` from the ``RESOURCEY`` env prefix."""
    from resourcey.util.env_parser import from_env

    return from_env(EncryptionKeysConfig, prefix="RESOURCEY")  # type: ignore[no-any-return]


def clear_encryption_service_cache() -> None:
    """Drop the cached singleton so the next call rebuilds.

    Intended for tests that flip encryption env vars between cases; not for
    runtime use.
    """
    get_encryption_service.cache_clear()
