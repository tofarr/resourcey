"""Opaque, tamper-proof cursor for keyset pagination (issue #35).

The cursor encodes the sort key (and id, for stable tie-breaking) of the
last row on the current page, **plus the ``(sort_field, ascending)`` it was
built for** so the service can reject a cursor reused under a different
sort. It is encrypted with the existing
:class:`~resourcey.encryption.encryption_service.EncryptionService` (JWE
``dir`` + ``A256GCM``), so a client cannot forge or alter it: any tampering
invalidates the GCM auth tag and ``decrypt_value`` raises, which the service
maps to ``400 invalid_input``.

Sort-key and id values are stored with a type tag (``_serialize`` /
``_deserialize``) so non-JSON-native types (``datetime``, ``UUID``,
``Decimal``, ...) round-trip back to their native Python type. The repository
then binds a correctly-typed value to the keyset ``WHERE`` predicate, which
matters on Postgres (a ``timestamp`` column compared to a ``text`` bind
param raises instead of coercing).

A *seek* (keyset) cursor is both efficient (no ``OFFSET n`` scan) and stable
under concurrent inserts. The repository resolves it into a ``WHERE``
predicate (``(sort_key, id) > (cursor_key, cursor_id)`` for ascending;
mirrored for descending) rather than an offset.
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
    from sqlalchemy.sql.selectable import Select

    from resourcey.encryption.encryption_service import EncryptionService


# JSON keys kept short to minimise ciphertext size.
_K_SORT = "s"  # sort field name (None for the default id-ordered case)
_K_ASC = "a"  # ascending flag
_K_KEY = "k"  # type-tagged sort-key value
_K_ID = "id"  # type-tagged id value


def _serialize(value: Any) -> tuple[str, Any]:
    """Tag a value with its type so it can be deserialized natively.

    Returns a ``(type_tag, json_native_repr)`` pair. ``json_native_repr`` is
    always a JSON-native scalar (str/int/float/bool) so the encrypted payload
    stays plain JSON; the tag drives reconstruction of the native Python type
    on decode (e.g. ``datetime`` from its ISO string). This keeps the value
    bound to the SQLAlchemy column correctly typed (matters for Postgres,
    which will not implicitly cast a ``text`` param to ``timestamp``).
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
    # Fallback: stringify so we never fail to encode; the comparison may then
    # be string-based (acceptable for unknown custom types).
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
    # Unknown tag: fall back to the raw repr.
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
    encryption_service: EncryptionService,
    cursor: str,
) -> tuple[str | None, bool, Any, Any]:
    """Decrypt a cursor back to ``(sort_field, ascending, sort_key, id_value)``.

    ``sort_field`` is ``None`` when the cursor was built for the default
    id-ordered (no-sort) case. Raises ``ValueError`` (from ``decrypt_value``)
    when the cursor is malformed or tampered; the service maps that to
    ``400 invalid_input``.
    """
    plaintext = encryption_service.decrypt_value(cursor)
    payload: dict[str, Any] = json.loads(plaintext)
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

    For ascending order, rows where ``(sort_key, id) > (cursor_key, cursor_id)``
    are kept. For descending, the comparison is mirrored so rows *before* the
    cursor (in sort order) are kept — i.e. ``(sort_key, id) < (cursor_key, cursor_id)``.
    The id tie-breaker keeps the order stable when sort keys collide.

    When the sort column *is* the id column (the default no-sort case), the
    predicate collapses to a single comparison on id.
    """
    if sort_column is id_column:
        if ascending:
            return cast("ColumnElement[bool]", sort_column > cursor_id)
        return cast("ColumnElement[bool]", sort_column < cursor_id)
    if ascending:
        return or_(
            sort_column > cursor_key,
            and_(sort_column == cursor_key, id_column > cursor_id),
        )
    return or_(
        sort_column < cursor_key,
        and_(sort_column == cursor_key, id_column < cursor_id),
    )


def apply_cursor(
    stmt: Select[Any],
    model: Any,
    *,
    sort: tuple[str, bool] | None,
    id_field: str,
    cursor_key: Any,
    cursor_id: Any,
) -> Select[Any]:
    """Apply the keyset ``WHERE`` predicate for a decoded cursor.

    ``sort`` is the validated ``(field_name, ascending)`` tuple (or ``None``
    for the default id-ordered case). The cursor key/id are the decrypted
    values from :func:`decode_cursor`.
    """
    id_column = getattr(model, id_field)
    if sort is None:
        return stmt.where(
            keyset_predicate(
                sort_column=id_column,
                id_column=id_column,
                cursor_key=cursor_key,
                cursor_id=cursor_id,
                ascending=True,
            )
        )
    field_name, ascending = sort
    sort_column = getattr(model, field_name)
    return stmt.where(
        keyset_predicate(
            sort_column=sort_column,
            id_column=id_column,
            cursor_key=cursor_key,
            cursor_id=cursor_id,
            ascending=ascending,
        )
    )
