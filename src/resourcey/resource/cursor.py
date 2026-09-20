"""Opaque, tamper-proof cursor for keyset pagination (issue #35).

The cursor encodes the sort key (and id, for stable tie-breaking) of the
last row on the current page. It is encrypted with the existing
:class:`~resourcey.encryption.encryption_service.EncryptionService` (JWE
``dir`` + ``A256GCM``), so a client cannot forge or alter it: any tampering
invalidates the GCM auth tag and ``decrypt_value`` raises, which the service
maps to ``400 invalid_input``.

A *seek* (keyset) cursor is both efficient (no ``OFFSET n`` scan) and stable
under concurrent inserts. The repository resolves it into a ``WHERE``
predicate (``(sort_key, id) > (cursor_key, cursor_id)`` for ascending;
mirrored for descending) rather than an offset.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, or_

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import Select

    from resourcey.encryption.encryption_service import EncryptionService


# JSON keys kept short to minimise ciphertext size.
_K_KEY = "k"
_K_ID = "id"


def encode_cursor(
    encryption_service: EncryptionService,
    *,
    sort_key: Any,
    id_value: Any,
) -> str:
    """Encrypt a ``(sort_key, id)`` pair into an opaque cursor string."""
    payload = json.dumps({_K_KEY: sort_key, _K_ID: id_value}, default=str)
    return encryption_service.encrypt_value(payload)


def decode_cursor(
    encryption_service: EncryptionService,
    cursor: str,
) -> tuple[Any, Any]:
    """Decrypt a cursor back to ``(sort_key, id_value)``.

    Raises ``ValueError`` (from ``decrypt_value``) when the cursor is
    malformed or tampered; the service maps that to ``400 invalid_input``.
    """
    plaintext = encryption_service.decrypt_value(cursor)
    payload: dict[str, Any] = json.loads(plaintext)
    return payload[_K_KEY], payload[_K_ID]


def keyset_predicate(
    model: Any,
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
        return cast(
            "ColumnElement[bool]", sort_column > cursor_id if ascending else sort_column < cursor_id
        )
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
                model,
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
            model,
            sort_column=sort_column,
            id_column=id_column,
            cursor_key=cursor_key,
            cursor_id=cursor_id,
            ascending=ascending,
        )
    )
