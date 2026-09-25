"""Opaque, tamper-proof keyset cursor for ``v2`` pagination (issues #78, #97).

The cursor encodes the ``(sort field, ascending, sort key, id)`` of the last row
on the current page — the ``v1`` shape — and is encrypted with
:class:`~resourcey.v2.encryption.encryption_service.EncryptionService`
(JWE ``dir`` + ``A256GCM``), so a client cannot forge or alter it: any tampering
invalidates the GCM auth tag and :meth:`EncryptionService.decrypt_value` raises,
which the service maps to an error rather than silently paging from a bogus
position.

Encoding the sort field and direction is what lets the service **reject** a
cursor reused under a different sort (a ``sort=created_at`` page's
``next_cursor`` sent back with ``sort=name`` would otherwise apply the
decrypted key against the wrong column, yielding silently wrong results).
``sort_field`` is ``None`` for the default, no-sort (identifier-ordered) case.

Values are stored with a type tag (:func:`_serialize` / :func:`_deserialize`) so
non-JSON-native types (``datetime``, ``UUID``, ``Decimal``, ...) round-trip to
their native Python type and bind correctly to the keyset ``WHERE`` predicate.

A *seek* (keyset) cursor is both efficient (no ``OFFSET n`` scan) and stable
under concurrent inserts.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, or_

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

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


def keyset_predicate(
    *,
    sort_column: Any,
    id_column: Any,
    cursor_key: Any,
    cursor_id: Any,
    ascending: bool,
) -> ColumnElement[bool]:
    """Build the ``WHERE`` clause that seeks past the cursor row.

    Ordering is ``(sort_key, id)`` with the sort key in the requested direction
    and the identifier *always ascending* (the tie-breaker :meth:`SqlSortConverter.apply`
    appends). Ascending therefore keeps ``sort_key > cursor_key``, or an equal
    key with a greater id. Descending keeps ``sort_key < cursor_key``, or an
    equal key with a **greater** id: only the sort-key comparison mirrors, never
    the tie-breaker — mirroring the id too would skip a row.

    When the sort column *is* the id column (the default no-sort case), the
    predicate collapses to a single comparison on id.

    A ``None`` ``cursor_key`` means the cursor row sits in the null block (NULLs
    first ascending, last descending — the ordering :meth:`SqlSortConverter.apply`
    emits). Such a row is seeked past with a NULL test rather than a ``= NULL``
    comparison, which would match nothing.
    """
    if sort_column is id_column:
        if ascending:
            return cast("ColumnElement[bool]", sort_column > cursor_id)
        return cast("ColumnElement[bool]", sort_column < cursor_id)
    # The sort column's own comparison mirrors for descending; the id tie-breaker
    # never does. NULL placement reverses with the direction, so each direction
    # needs its own null-block branch to stay a correct keyset walk.
    if ascending:
        if cursor_key is None:
            return cast(
                "ColumnElement[bool]",
                sort_column.is_not(None) | (sort_column.is_(None) & (id_column > cursor_id)),
            )
        return or_(
            sort_column > cursor_key,
            and_(sort_column == cursor_key, id_column > cursor_id),
        )
    if cursor_key is None:
        return cast("ColumnElement[bool]", sort_column.is_(None) & (id_column > cursor_id))
    return or_(
        sort_column < cursor_key,
        and_(sort_column == cursor_key, id_column > cursor_id),
        sort_column.is_(None),
    )
