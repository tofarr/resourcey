"""Opaque, tamper-proof keyset cursor codec for ``v2`` pagination (issues #78, #97, #80).

The cursor encodes the ``(sort field, ascending, sort key, id)`` of the last row
on the current page — the ``v1`` shape — and is encrypted with
:class:`~resourcey.v2.encryption.encryption_service.EncryptionService`
(JWE ``dir`` + ``A256GCM``), so a client cannot forge or alter it: any tampering
invalidates the GCM auth tag and :meth:`EncryptionService.decrypt_value` raises,
which the service maps to an error rather than silently paging from a bogus
position.

Encoding the sort field and direction is what lets a service **reject** a cursor
reused under a different sort (a ``sort=created_at`` page's ``next_cursor`` sent
back with ``sort=name`` would otherwise apply the decrypted key against the
wrong column, yielding silently wrong results). ``sort_field`` is ``None`` for
the default, no-sort (identifier-ordered) case.

Values are stored with a type tag (:func:`_serialize` / :func:`_deserialize`) so
non-JSON-native types (``datetime``, ``UUID``, ``Decimal``, ...) round-trip to
their native Python type and bind correctly to the backend's keyset predicate.

This module holds the *storage-agnostic* half only: the SQL keyset ``WHERE``
predicate stays in :mod:`resourcey.v2.sql.cursor` (it imports SQLAlchemy) and the
Mongo one in ``v2/mongo``, so the tamper-proof encoding is identical across
backends while each backend builds its own seek clause.

It is part of the ``v2/util`` bottom layer: besides the standard library it
imports no project package at runtime (the encryption service is a
``TYPE_CHECKING``-only reference).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from resourcey.v2.encryption.encryption_service import EncryptionService

# JSON keys kept short to minimise ciphertext size.
_K_SORT = "s"  # sort field name (None for the default id-ordered case)
_K_ASC = "a"  # ascending flag
_K_KEY = "k"  # type-tagged sort-key value
_K_ID = "id"  # type-tagged id value


def _serialize(value: Any) -> tuple[str, Any]:
    """Tag a value with its type so it can be deserialized natively.

    Returns a ``(type_tag, json_native_repr)`` pair whose repr is always a
    JSON-native scalar, keeping the encrypted payload plain JSON while the tag
    drives reconstruction of the native Python type on decode.

    ``None`` gets its own tag: stringifying it to ``"None"`` (the previous
    fallback) decoded back as the *string* ``"None"``, which bound against the
    wrong type and broke keyset paging on a nullable sort column.
    """
    if value is None:
        return "n", None
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
    if tag == "n":
        return None
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


def encode_cursor(
    encryption_service: EncryptionService,
    *,
    sort_field: str | None,
    ascending: bool,
    sort_key: Any,
    id_value: Any,
) -> str:
    """Encrypt a ``(sort_field, ascending, sort_key, id)`` tuple into a cursor."""
    payload = json.dumps(
        {
            _K_SORT: sort_field,
            _K_ASC: ascending,
            _K_KEY: _serialize(sort_key),
            _K_ID: _serialize(id_value),
        },
        default=str,
    )
    return encryption_service.encrypt_value(payload)


def decode_cursor(
    encryption_service: EncryptionService, cursor: str
) -> tuple[str | None, bool, Any, Any]:
    """Decrypt a cursor back to ``(sort_field, ascending, sort_key, id_value)``.

    ``sort_field`` is ``None`` when the cursor was built for the default
    identifier-ordered (no-sort) case. Raises ``ValueError`` (from
    ``decrypt_value``) when the cursor is malformed or tampered.
    """
    payload: dict[str, Any] = json.loads(encryption_service.decrypt_value(cursor))
    key_tag, key_repr = payload[_K_KEY]
    id_tag, id_repr = payload[_K_ID]
    return (
        payload[_K_SORT],
        bool(payload[_K_ASC]),
        _deserialize(key_tag, key_repr),
        _deserialize(id_tag, id_repr),
    )
