"""Opaque, tamper-proof keyset cursor for ``v2`` pagination (issue #78).

The cursor encodes the identifier of the last row on the current page and is
encrypted with :class:`~resourcey.v2.encryption.encryption_service.EncryptionService`
(JWE ``dir`` + ``A256GCM``), so a client cannot forge or alter it: any tampering
invalidates the GCM auth tag and :meth:`EncryptionService.decrypt_value` raises,
which the service maps to an error rather than silently paging from a bogus
position.

Ordering in ``v2`` is fixed to the identifier field, so the cursor needs only
the id keyset — no sort-field tuple. Values are stored with a type tag
(:func:`_serialize` / :func:`_deserialize`) so non-JSON-native types
(``datetime``, ``UUID``, ``Decimal``, ...) round-trip to their native Python
type and bind correctly to the keyset ``WHERE`` predicate.

A *seek* (keyset) cursor is both efficient (no ``OFFSET n`` scan) and stable
under concurrent inserts.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from resourcey.v2.encryption.encryption_service import EncryptionService

# JSON keys kept short to minimise ciphertext size.
_K_ID = "id"  # type-tagged id value


def _serialize(value: Any) -> tuple[str, Any]:
    """Tag a value with its type so it can be deserialized natively.

    Returns a ``(type_tag, json_native_repr)`` pair whose repr is always a
    JSON-native scalar, keeping the encrypted payload plain JSON while the tag
    drives reconstruction of the native Python type on decode.
    """
    if isinstance(value, bool):
        return "b", value
    if isinstance(value, int):
        return "i", value
    if isinstance(value, float):
        return "f", value
    if isinstance(value, str):
        return "s", value
    if isinstance(value, datetime):
        return "dt", value.isoformat()
    if isinstance(value, date):
        return "d", value.isoformat()
    if isinstance(value, time):
        return "t", value.isoformat()
    if isinstance(value, UUID):
        return "u", str(value)
    if isinstance(value, Decimal):
        return "dec", str(value)
    return "s", str(value)


def _deserialize(tag: str, repr_: Any) -> Any:
    """Reconstruct the native Python value from its ``(tag, repr)`` pair."""
    if tag == "b":
        return bool(repr_)
    if tag == "i":
        return int(repr_)
    if tag == "f":
        return float(repr_)
    if tag == "s":
        return str(repr_)
    if tag == "dt":
        return datetime.fromisoformat(str(repr_))
    if tag == "d":
        return date.fromisoformat(str(repr_))
    if tag == "t":
        return time.fromisoformat(str(repr_))
    if tag == "u":
        return UUID(str(repr_))
    if tag == "dec":
        return Decimal(str(repr_))
    return repr_


def encode_cursor(encryption_service: EncryptionService, id_value: Any) -> str:
    """Encrypt an id value into an opaque cursor."""
    payload = json.dumps({_K_ID: _serialize(id_value)}, default=str)
    return encryption_service.encrypt_value(payload)


def decode_cursor(encryption_service: EncryptionService, cursor: str) -> Any:
    """Decrypt a cursor back to its native id value.

    Raises ``ValueError`` (from ``decrypt_value``) when the cursor is malformed
    or tampered.
    """
    payload: dict[str, Any] = json.loads(encryption_service.decrypt_value(cursor))
    id_tag, id_repr = payload[_K_ID]
    return _deserialize(id_tag, id_repr)


def keyset_predicate(id_column: Any, cursor_id: Any) -> ColumnElement[bool]:
    """The ``WHERE`` clause that seeks past the cursor row (ascending id order)."""
    return id_column > cursor_id  # type: ignore[no-any-return]
