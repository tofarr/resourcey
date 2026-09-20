"""Unit tests for the cursor module (issue #35).

Covers encode/decode round-trips, tamper detection, and the keyset predicate
builder. The cursor module is the encryption boundary for keyset pagination —
these tests pin its contract independently of the service/repository layers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, select
from sqlalchemy.orm import DeclarativeBase

from resourcey.encryption.encryption_service import (
    clear_encryption_service_cache,
    get_encryption_service,
)
from resourcey.resource.cursor import apply_cursor, decode_cursor, encode_cursor, keyset_predicate


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "cursor_test_rows"
    id = Column(Integer, primary_key=True)
    size = Column(Integer)


@pytest.fixture
def enc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "test-secret-key-for-cursors")
    clear_encryption_service_cache()
    return get_encryption_service()


class TestEncodeDecode:
    def test_round_trip_int_keys(self, enc) -> None:
        token = encode_cursor(enc, sort_key=42, id_value=7)
        assert isinstance(token, str)
        assert decode_cursor(enc, token) == (42, 7)

    def test_round_trip_string_keys(self, enc) -> None:
        token = encode_cursor(enc, sort_key="middle", id_value=99)
        assert decode_cursor(enc, token) == ("middle", 99)

    def test_round_trip_float_keys(self, enc) -> None:
        token = encode_cursor(enc, sort_key=3.14, id_value=1)
        assert decode_cursor(enc, token) == (3.14, 1)

    def test_token_is_opaque(self, enc) -> None:
        """The cursor must not leak the sort key/id as plaintext."""
        token = encode_cursor(enc, sort_key=12345, id_value=67890)
        # A JWE token is three dot-separated base64url segments; the plaintext
        # payload is encrypted, so the numeric values must not appear in it.
        assert "12345" not in token
        assert "67890" not in token

    def test_decode_tampered_token_raises(self, enc) -> None:
        token = encode_cursor(enc, sort_key=1, id_value=1)
        # Flip a character in the ciphertext segment to break AEAD auth.
        tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(enc, tampered)

    def test_decode_garbage_raises(self, enc) -> None:
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(enc, "not-a-jwe-token")

    def test_decode_wrong_key_raises(self, enc, monkeypatch: pytest.MonkeyPatch) -> None:
        token = encode_cursor(enc, sort_key=1, id_value=1)
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_ID", "other")
        monkeypatch.setenv("RESOURCEY_ENCRYPTION_KEY_VALUE", "a-different-secret-key-entirely")
        clear_encryption_service_cache()
        other = get_encryption_service()
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(other, token)


class TestKeysetPredicate:
    def test_ascending_predicate(self) -> None:
        pred = keyset_predicate(
            _Row,
            sort_column=_Row.size,
            id_column=_Row.id,
            cursor_key=10,
            cursor_id=1,
            ascending=True,
        )
        stmt = select(_Row).where(pred)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert ">" in compiled
        assert "10" in compiled
        assert "1" in compiled

    def test_descending_predicate(self) -> None:
        pred = keyset_predicate(
            _Row,
            sort_column=_Row.size,
            id_column=_Row.id,
            cursor_key=10,
            cursor_id=1,
            ascending=False,
        )
        stmt = select(_Row).where(pred)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "<" in compiled

    def test_apply_cursor_returns_statement(self) -> None:
        stmt = select(_Row)
        result = apply_cursor(
            stmt,
            _Row,
            sort=("size", True),
            id_field="id",
            cursor_key=5,
            cursor_id=2,
        )
        # apply_cursor returns a statement with the keyset WHERE predicate.
        compiled = str(result.compile(compile_kwargs={"literal_binds": True}))
        assert "5" in compiled
        assert "2" in compiled

    def test_apply_cursor_without_sort_uses_id(self) -> None:
        """When sort is None the cursor keys off the id column alone."""
        stmt = select(_Row)
        result = apply_cursor(
            stmt,
            _Row,
            sort=None,
            id_field="id",
            cursor_key=99,
            cursor_id=99,
        )
        compiled = str(result.compile(compile_kwargs={"literal_binds": True}))
        assert "99" in compiled
