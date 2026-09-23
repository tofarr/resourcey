"""Tests for the ``v2/encryption`` package (issue #78).

Covers the migrated ``EncryptionService`` (value + JWE-token paths), the key
config (including the ``encryption_key`` / ``decryption_keys`` rotation model
and the ``kid`` header), the ``dir`` + ``A256GCM``-only registry, and
construction from an injected config instance (no env singleton).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from resourcey.v2.encryption.encryption_config import (
    EncryptionKeyConfig,
    EncryptionKeysConfig,
)
from resourcey.v2.encryption.encryption_service import EncryptionService, utc_now


def _service(
    key_id: str = "test", secret: str = "test-secret-key-for-cursors"
) -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id=key_id, value=secret))
    )


# ---------------------------------------------------------------------------
# Key config
# ---------------------------------------------------------------------------


def test_encryption_key_is_auto_included_in_decryption_keys():
    config = EncryptionKeysConfig(
        encryption_key=EncryptionKeyConfig(id="primary", value="s"),
        decryption_keys=[EncryptionKeyConfig(id="old", value="o")],
    )
    assert [k.id for k in config.decryption_keys] == ["primary", "old"]


def test_existing_encryption_key_is_not_duplicated():
    enc = EncryptionKeyConfig(id="primary", value="s")
    config = EncryptionKeysConfig(encryption_key=enc, decryption_keys=[enc])
    assert len(config.decryption_keys) == 1


def test_key_serialization_redacts_by_default():
    config = EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id="k", value="super-secret"))
    dumped = config.model_dump()
    assert "super-secret" not in str(dumped)


def test_key_serialization_exposes_secrets_with_context():
    config = EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id="k", value="super-secret"))
    dumped = config.model_dump(context={"expose_secrets": True})
    assert dumped["encryption_key"]["value"] == "super-secret"


# ---------------------------------------------------------------------------
# Value encryption
# ---------------------------------------------------------------------------


def test_value_round_trips():
    service = _service()
    assert service.decrypt_value(service.encrypt_value("hello")) == "hello"


def test_value_ciphertext_is_opaque_and_kid_tagged():
    service = _service(key_id="rotated")
    ciphertext = service.encrypt_value("hello")
    assert "hello" not in ciphertext
    assert ciphertext.count(".") == 4  # JWE compact serialization
    assert service.encryption_key_id == "rotated"


def test_value_uses_the_a256gcm_registry_only():
    service = _service()
    import base64
    import json

    header_b64 = service.encrypt_value("x").split(".")[0]
    padded = header_b64 + "=" * (-len(header_b64) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded))
    assert header["alg"] == "dir"
    assert header["enc"] == "A256GCM"


def test_decryption_selects_the_key_from_the_kid_header():
    # A value encrypted under an old key still decrypts after rotation.
    old = EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id="old", value="old-secret"))
    old_ciphertext = EncryptionService(old).encrypt_value("legacy")

    rotated = EncryptionKeysConfig(
        encryption_key=EncryptionKeyConfig(id="new", value="new-secret"),
        decryption_keys=[old.encryption_key],
    )
    service = EncryptionService(rotated)
    assert service.decryption_key_ids == ["new", "old"]
    assert service.decrypt_value(old_ciphertext) == "legacy"
    assert service.decrypt_value(service.encrypt_value("fresh")) == "fresh"


def test_unknown_kid_raises():
    service = _service(key_id="known")
    ciphertext = service.encrypt_value("x")
    other = _service(key_id="other")
    with pytest.raises(ValueError, match="not found"):
        other.decrypt_value(ciphertext)


def test_tampered_ciphertext_is_rejected():
    service = _service()
    ciphertext = service.encrypt_value("hello")
    segments = ciphertext.split(".")
    segments[3] = ("A" if segments[3][0] != "A" else "B") + segments[3][1:]
    with pytest.raises(ValueError, match="decryption failed"):
        service.decrypt_value(".".join(segments))


def test_malformed_ciphertext_is_rejected():
    service = _service()
    with pytest.raises(ValueError, match="Invalid JWE token format"):
        service.decrypt_value("not-a-jwe")


def test_token_without_kid_is_rejected():
    import base64
    import json

    service = _service()
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "dir", "enc": "A256GCM"}).encode()
    ).decode()
    token = ".".join([header.rstrip("="), "", "", "", ""])
    with pytest.raises(ValueError, match="kid"):
        service.decrypt_value(token)


# ---------------------------------------------------------------------------
# JWE token encryption (auth path)
# ---------------------------------------------------------------------------


def test_jwe_token_round_trips_claims():
    service = _service()
    token = service.create_jwe_token({"sub": "user-1", "ttyp": "access"})
    claims = service.decrypt_jwe_token(token)
    assert claims["sub"] == "user-1"
    assert claims["ttyp"] == "access"
    assert "iat" in claims


def test_jwe_token_expiry_claim():
    service = _service()
    token = service.create_jwe_token({"sub": "u"}, expires_in=timedelta(minutes=5))
    claims = service.decrypt_jwe_token(token)
    assert claims["exp"] > claims["iat"]


def test_jwe_token_unknown_kid_raises():
    token = _service(key_id="a").create_jwe_token({"sub": "u"})
    with pytest.raises(ValueError, match="not found"):
        _service(key_id="b").decrypt_jwe_token(token)


def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo is not None
