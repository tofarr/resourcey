"""Translate a standard :class:`SortOrder` into SQLAlchemy ordering (issue #97).

This is the only ``v2`` file that imports SQLAlchemy for sorting. Dispatch is a
single registry keyed on the :class:`SortOrder` type, mirroring
``v2/sql/filter_converter.py``: because the node set is a discriminated union
the registry is enumerable, and the module asserts at import time that every
standard node has a handler — a new node added without one fails at startup, not
per request.

:meth:`SqlSortConverter.apply` appends the identifier as a tie-breaker to every
ordering (``<sort> asc/desc, id asc``), so keyset paging is a total order even
when sort keys collide. The matching keyset predicate lives in
:mod:`resourcey.v2.sql.cursor`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from sqlalchemy import Column, ColumnElement

from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.util.sort_order import AttrSortOrder, SortOrder

S = TypeVar("S")

# A sort handler: ``(ctx, node) -> order-by expression``.
SortHandler = Callable[["SqlSortContext", SortOrder[Any]], ColumnElement[Any]]


def _attr_order(ctx: SqlSortContext, node: SortOrder[Any]) -> ColumnElement[Any]:
    """Order by one column, ascending or descending (``node`` is an ``AttrSortOrder``)."""
    column = ctx.column_for(node.attribute)  # type: ignore[attr-defined]
    return cast("ColumnElement[Any]", column.desc() if node.descending else column.asc())  # type: ignore[attr-defined]


_REGISTRY: dict[type[SortOrder[Any]], SortHandler] = {AttrSortOrder: _attr_order}


def register_sort_order(sort_type: type[SortOrder[Any]], handler: SortHandler) -> None:
    """Register a handler for a :class:`SortOrder` node type."""
    _REGISTRY[sort_type] = handler


def _assert_registry_is_complete() -> None:
    """Fail at import time if a standard node has no handler."""
    standard = {AttrSortOrder}
    missing = standard - set(_REGISTRY)
    if missing:
        raise RuntimeError(
            f"SqlSortConverter registry is incomplete: {sorted(t.__name__ for t in missing)}"
        )


_assert_registry_is_complete()


@dataclass(frozen=True)
class SqlSortContext:
    """Everything a sort handler needs, passed as one frozen object.

    Args:
        columns: DTO attribute name -> the table column it maps to. Only the
            resource's *sortable* fields are present, so a field projected away
            from the read model cannot be sorted on (``?sort=secret`` would leak
            the hidden value's relative order).
        id_column: The identifier column, appended as the stable tie-breaker.
    """

    columns: Mapping[str, Column[Any]]
    id_column: Column[Any]

    def column_for(self, attribute: str) -> Column[Any]:
        """The column for ``attribute``; raise :class:`InvalidInputError` if absent."""
        try:
            return self.columns[attribute]
        except KeyError:
            raise InvalidInputError(f"Unknown or non-sortable sort field {attribute!r}") from None


class SqlSortConverter:
    """Convert a standard :class:`SortOrder` into a SQLAlchemy ``ORDER BY``."""

    def __init__(self, ctx: SqlSortContext) -> None:
        self.ctx = ctx

    def apply(self, stmt: S, sort_order: SortOrder[Any]) -> S:
        """Return ``stmt`` ordered by ``sort_order`` then the identifier."""
        try:
            handler = _REGISTRY[type(sort_order)]
        except KeyError:
            raise InvalidInputError(
                f"No SQL conversion is registered for {type(sort_order).__name__}"
            ) from None
        ordering = [handler(self.ctx, sort_order), self.ctx.id_column.asc()]
        return cast("S", stmt.order_by(*ordering))  # type: ignore[attr-defined]
