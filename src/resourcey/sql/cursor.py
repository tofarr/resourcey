"""SQL keyset (seek) cursor predicate for pagination (issues #78, #97).

The tamper-proof cursor *codec* lives in :mod:`resourcey.util.cursor`, so a
non-SQL backend reuses the identical encoding without importing SQLAlchemy. This
module holds the SQLAlchemy half — :func:`keyset_predicate`, the ``WHERE``
clause that seeks past the cursor row — plus a re-export of the codec.

A *seek* (keyset) cursor is both efficient (no ``OFFSET n`` scan) and stable
under concurrent inserts.

This module imports no code outside the framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, or_

from resourcey.util.cursor import decode_cursor as decode_cursor
from resourcey.util.cursor import encode_cursor as encode_cursor

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement


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
    and the identifier *always ascending*. Only the sort-key comparison mirrors
    for descending, never the tie-breaker — mirroring the id too would skip a row.

    When the sort column *is* the id column, the predicate collapses to a single
    comparison on id.

    A ``None`` ``cursor_key`` means the cursor row sits in the null block (NULLs
    first ascending, last descending), so it is seeked past with a NULL test
    rather than a ``= NULL`` comparison.
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
