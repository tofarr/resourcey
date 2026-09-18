"""Tests for encryption key configuration and the JWE ``EncryptionService``.

Covers ``EncryptionKeyConfig`` (default id, redacted / exposed serialization),
``EncryptionKeysConfig`` (encryption key auto-included in decryption keys,
explicit rotation list preserved, required encryption key),
``EncryptionService`` (round-trip, ``kid`` header selects the decryption key,
unknown ``kid`` raises, rotation after key change, env loading via the
``RESOURCEY`` prefix, cached singleton accessor), and the
``secret_serialization`` convention (encryption > plaintext > redacted
precedence on dump, decrypt-on-load, secret-map helpers).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import (
    EncryptionService,
    clear_encryption_service_cache,
    get_encryption_service,
)
from resourcey.util.env_parser import from_env
from resourcey.util.secret_serialization import (
    decrypt_secret_map,
    dump_secret_map,
    dump_secret_str,
    encrypt_secret_map,
    encryption_service_from_context,
    expose_secrets_from_context,
    load_secret_map,
    load_secret_str,
)

PLAINTEXT = "super-secret-token"


def _service(secret: str = "secret-one", key_id: str = "default") -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id=key_id, value=SecretStr(secret)),
        )
    )


def _service_with_rotation(
    enc_id: str, enc_secret: str, old_id: str, old_secret: str
) -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id=enc_id, value=SecretStr(enc_secret)),
            decryption_keys=[
                EncryptionKeyConfig(id=enc_id, value=SecretStr(enc_secret)),
                EncryptionKeyConfig(id=old_id, value=SecretStr(old_secret)),
            ],
        )
    )


class _FakeInfo:
    """Minimal stand-in for a pydantic SerializationInfo / ValidationInfo."""

    def __init__(self, context: dict | None) -> None:
        self.context = context


# ---------------------------------------------------------------------------
# EncryptionKeyConfig
# ---------------------------------------------------------------------------


class TestEncryptionKeyConfig:
    def test_default_id(self):
        cfg = EncryptionKeyConfig(value=SecretStr("x"))
        assert cfg.id == "default"

    def test_explicit_id(self):
        cfg = EncryptionKeyConfig(id="k1", value=SecretStr("x"))
        assert cfg.id == "k1"

    def test_value_redacted_by_default(self):
        cfg = EncryptionKeyConfig(id="k1", value=SecretStr("topsecret"))
        dumped = cfg.model_dump()
        assert dumped["value"] == "**********"

    def test_value_exposed_with_context(self):
        cfg = EncryptionKeyConfig(id="k1", value=SecretStr("topsecret"))
        dumped = cfg.model_dump(context={"expose_secrets": True})
        assert dumped["value"] == "topsecret"


# ---------------------------------------------------------------------------
# EncryptionKeysConfig
# ---------------------------------------------------------------------------


class TestEncryptionKeysConfig:
    def test_encryption_key_auto_included_in_decryption_keys(self):
        cfg = EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="k1", value=SecretStr("s1")),
        )
        assert [k.id for k in cfg.decryption_keys] == ["k1"]

    def test_rotation_list_preserved(self):
        cfg = EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="k1", value=SecretStr("s1")),
            decryption_keys=[EncryptionKeyConfig(id="k0", value=SecretStr("s0"))],
        )
        # Encryption key prepended since it was not in the supplied list.
        assert [k.id for k in cfg.decryption_keys] == ["k1", "k0"]

    def test_encryption_key_not_duplicated_when_already_present(self):
        cfg = EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="k1", value=SecretStr("s1")),
            decryption_keys=[EncryptionKeyConfig(id="k1", value=SecretStr("s1"))],
        )
        assert [k.id for k in cfg.decryption_keys] == ["k1"]

    def test_encryption_key_required(self):
        with pytest.raises(ValidationError):
            EncryptionKeysConfig()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# EncryptionService
# ---------------------------------------------------------------------------


class TestEncryptionServiceRoundTrip:
    def test_encrypt_decrypt_round_trip(self):
        svc = _service()
        ciphertext = svc.encrypt_value(PLAINTEXT)
        assert ciphertext != PLAINTEXT
        assert svc.decrypt_value(ciphertext) == PLAINTEXT

    def test_ciphertext_is_jwe_compact(self):
        svc = _service(key_id="k1")
        ciphertext = svc.encrypt_value(PLAINTEXT)
        # JWE compact serialization has exactly five dot-separated segments.
        assert ciphertext.count(".") == 4

    def test_kid_header_carries_encryption_key_id(self):
        svc = _service(key_id="k1")
        ciphertext = svc.encrypt_value(PLAINTEXT)
        # The kid is the first (protected header) segment, base64url JSON.
        import base64
        import json

        header_b64 = ciphertext.split(".")[0]
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["kid"] == "k1"
        assert header["alg"] == "dir"
        assert header["enc"] == "A256GCM"

    def test_encryption_key_id_property(self):
        svc = _service(key_id="k1")
        assert svc.encryption_key_id == "k1"

    def test_decryption_key_ids_property(self):
        svc = _service_with_rotation("k1", "s1", "k0", "s0")
        assert svc.decryption_key_ids == ["k1", "k0"]


class TestEncryptionServiceKeySelection:
    def test_kid_header_selects_decryption_key(self):
        svc = _service_with_rotation("k1", "s1", "k0", "s0")
        # Encrypt under the old key directly.
        old_svc = _service(secret="s0", key_id="k0")
        ciphertext = old_svc.encrypt_value(PLAINTEXT)
        # The rotation-aware service can still decrypt it.
        assert svc.decrypt_value(ciphertext) == PLAINTEXT

    def test_unknown_kid_raises(self):
        svc = _service(key_id="k1")
        old_svc = _service(secret="other", key_id="k0")
        ciphertext = old_svc.encrypt_value(PLAINTEXT)
        with pytest.raises(ValueError, match="k0"):
            svc.decrypt_value(ciphertext)

    def test_missing_kid_header_raises(self):
        svc = _service()
        # A JWE with no kid header: build one manually with an empty header.
        import hashlib
        import json

        from joserfc import jwe
        from joserfc.jwk import OctKey

        from resourcey.encryption.encryption_service import _JWE_REGISTRY

        key = OctKey.import_key(hashlib.sha256(b"secret-one").digest())
        header = {"alg": "dir", "enc": "A256GCM"}  # no kid
        ciphertext = jwe.encrypt_compact(
            header, json.dumps({"v": "x"}).encode(), key, registry=_JWE_REGISTRY
        )
        with pytest.raises(ValueError, match="kid"):
            svc.decrypt_value(ciphertext)

    def test_invalid_token_format_raises(self):
        svc = _service()
        with pytest.raises(ValueError, match="Invalid JWE token format"):
            svc.decrypt_value("not-a-jwe-token")

    def test_malformed_protected_header_raises(self):
        svc = _service()
        # Five dot-separated segments but the header is not valid base64url JSON.
        with pytest.raises(ValueError, match="Invalid JWE token format"):
            svc.decrypt_value("!!!.b.c.d.e")

    def test_tampered_ciphertext_raises(self):
        svc = _service()
        ciphertext = svc.encrypt_value(PLAINTEXT)
        # Flip a character in the ciphertext payload.
        tampered = ciphertext[:-2] + ("AA" if ciphertext[-2:] != "AA" else "BB")
        with pytest.raises(ValueError, match="decryption failed"):
            svc.decrypt_value(tampered)


class TestEncryptionServiceRotation:
    def test_value_encrypted_under_old_key_decrypts_after_rotation(self):
        old_svc = _service(secret="old-secret", key_id="k0")
        ciphertext = old_svc.encrypt_value("rotated-value")

        new_svc = _service_with_rotation("k1", "new-secret", "k0", "old-secret")
        assert new_svc.encryption_key_id == "k1"
        assert new_svc.decrypt_value(ciphertext) == "rotated-value"

    def test_new_encryption_uses_new_key_id(self):
        new_svc = _service_with_rotation("k1", "new-secret", "k0", "old-secret")
        ciphertext = new_svc.encrypt_value("fresh")
        import base64
        import json

        header_b64 = ciphertext.split(".")[0]
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["kid"] == "k1"


# ---------------------------------------------------------------------------
# Env loading + cached singleton
# ---------------------------------------------------------------------------


class TestEnvLoading:
    def test_loads_from_resourcey_prefix(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "env-k1")
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "env-secret")
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_ID", raising=False)
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", raising=False)
        cfg = from_env(EncryptionKeysConfig, prefix="RESOURCEY")
        assert cfg.encryption_key.id == "env-k1"
        assert cfg.encryption_key.value.get_secret_value() == "env-secret"
        assert cfg.decryption_keys[0].id == "env-k1"

    def test_loads_multiple_decryption_keys(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "k1")
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "s1")
        monkeypatch.setenv("RESOURCEY_DECRYPTION_KEYS_0_ID", "k0")
        monkeypatch.setenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", "s0")
        cfg = from_env(EncryptionKeysConfig, prefix="RESOURCEY")
        ids = [k.id for k in cfg.decryption_keys]
        assert ids == ["k1", "k0"]


class TestGetEncryptionServiceSingleton:
    def test_cached_singleton(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "k1")
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "singleton-secret")
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_ID", raising=False)
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", raising=False)
        clear_encryption_service_cache()
        first = get_encryption_service()
        second = get_encryption_service()
        assert first is second
        clear_encryption_service_cache()

    def test_singleton_round_trips(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "k1")
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "singleton-secret")
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_ID", raising=False)
        monkeypatch.delenv("RESOURCEY_DECRYPTION_KEYS_0_VALUE", raising=False)
        clear_encryption_service_cache()
        svc = get_encryption_service()
        ct = svc.encrypt_value("abc")
        assert svc.decrypt_value(ct) == "abc"
        clear_encryption_service_cache()


# ---------------------------------------------------------------------------
# secret_serialization convention
# ---------------------------------------------------------------------------


class TestSecretSerializationContext:
    def test_encryption_service_from_context_present(self):
        svc = _service()
        assert encryption_service_from_context(_FakeInfo({"encryption_service": svc})) is svc

    def test_encryption_service_from_context_missing(self):
        assert encryption_service_from_context(_FakeInfo({})) is None
        assert encryption_service_from_context(None) is None

    def test_encryption_service_from_context_non_service(self):
        # A non-service object is ignored (duck-typed).
        assert encryption_service_from_context(_FakeInfo({"encryption_service": "nope"})) is None

    def test_expose_secrets_from_context(self):
        assert expose_secrets_from_context(_FakeInfo({"expose_secrets": True})) is True
        assert expose_secrets_from_context(_FakeInfo({})) is False
        assert expose_secrets_from_context(None) is False


class TestDumpSecretStr:
    def test_redacted_with_no_context(self):
        assert dump_secret_str(SecretStr(PLAINTEXT)) == "**********"

    def test_redacted_with_empty_context(self):
        assert dump_secret_str(SecretStr(PLAINTEXT), _FakeInfo({})) == "**********"

    def test_encrypted_when_encryption_service_in_context(self):
        svc = _service()
        out = dump_secret_str(SecretStr(PLAINTEXT), _FakeInfo({"encryption_service": svc}))
        assert out != PLAINTEXT
        assert out != "**********"
        assert svc.decrypt_value(out) == PLAINTEXT

    def test_plaintext_when_expose_secrets(self):
        out = dump_secret_str(SecretStr(PLAINTEXT), _FakeInfo({"expose_secrets": True}))
        assert out == PLAINTEXT

    def test_encryption_wins_over_expose_secrets(self):
        svc = _service()
        ctx = _FakeInfo({"encryption_service": svc, "expose_secrets": True})
        out = dump_secret_str(SecretStr(PLAINTEXT), ctx)
        assert svc.decrypt_value(out) == PLAINTEXT
        assert out != PLAINTEXT


class TestLoadSecretStr:
    def test_decrypts_with_encryption_service(self):
        svc = _service()
        ciphertext = svc.encrypt_value(PLAINTEXT)
        assert load_secret_str(ciphertext, _FakeInfo({"encryption_service": svc})) == PLAINTEXT

    def test_passes_through_without_encryption_service(self):
        assert load_secret_str(PLAINTEXT, _FakeInfo({})) == PLAINTEXT
        assert load_secret_str(PLAINTEXT, None) == PLAINTEXT

    def test_accepts_secret_str_input(self):
        svc = _service()
        ciphertext = SecretStr(svc.encrypt_value(PLAINTEXT))
        assert load_secret_str(ciphertext, _FakeInfo({"encryption_service": svc})) == PLAINTEXT


class TestSecretMapHelpers:
    def test_dump_secret_map_redacted(self):
        out = dump_secret_map({"a": SecretStr("x")}, _FakeInfo({}))
        assert out == {"a": "**********"}

    def test_dump_secret_map_encrypted(self):
        svc = _service()
        out = dump_secret_map({"a": SecretStr("x")}, _FakeInfo({"encryption_service": svc}))
        assert svc.decrypt_value(out["a"]) == "x"

    def test_dump_secret_map_none(self):
        assert dump_secret_map(None, _FakeInfo({})) is None

    def test_load_secret_map_decrypts(self):
        svc = _service()
        ct = svc.encrypt_value("x")
        out = load_secret_map({"a": ct}, _FakeInfo({"encryption_service": svc}))
        assert out["a"].get_secret_value() == "x"

    def test_load_secret_map_none(self):
        assert load_secret_map(None, _FakeInfo({})) is None

    def test_encrypt_secret_map_with_service(self):
        svc = _service()
        out = encrypt_secret_map(svc, {"a": SecretStr("x")})
        assert svc.decrypt_value(out["a"]) == "x"

    def test_encrypt_secret_map_without_service(self):
        out = encrypt_secret_map(None, {"a": SecretStr("x")})
        assert out == {"a": "x"}

    def test_encrypt_secret_map_none(self):
        assert encrypt_secret_map(_service(), None) == {}

    def test_decrypt_secret_map_with_service(self):
        svc = _service()
        ct = svc.encrypt_value("x")
        out = decrypt_secret_map(svc, {"a": ct})
        assert out["a"].get_secret_value() == "x"

    def test_decrypt_secret_map_without_service(self):
        out = decrypt_secret_map(None, {"a": "x"})
        assert out["a"].get_secret_value() == "x"

    def test_decrypt_secret_map_none(self):
        assert decrypt_secret_map(_service(), None) == {}


# ---------------------------------------------------------------------------
# No openhands import
# ---------------------------------------------------------------------------


class TestNoOpenhandsImport:
    @pytest.mark.parametrize(
        "module",
        [
            "resourcey.encryption.encryption_config",
            "resourcey.encryption.encryption_service",
            "resourcey.util.secret_serialization",
        ],
    )
    def test_no_openhands_import(self, module):
        import importlib

        mod = importlib.import_module(module)
        source = Path(mod.__file__).read_text()
        assert "openhands" not in source.lower()
