"""Translate a standard :class:`SortOrder` into a Mongo sort spec (issue #80).

Mirrors :mod:`resourcey.v2.sql.sort_converter`: dispatch is a single registry
keyed on the :class:`SortOrder` type, and the module asserts at import time that
every standard node has a handler — a new node added without one fails at
startup, not per request.

:meth:`MongoSortConverter.apply` appends the identifier (``_id``) ascending as a
tie-breaker to every ordering (``<sort> 1/-1, _id 1``), so keyset paging is a
total order even when sort keys collide. The matching keyset predicate lives in
:mod:`resourcey.v2.mongo.mongo_service`.

NULL placement is not configured here: Mongo's per-type rule places ``null``
first ascending and last descending, which is exactly the in-memory
:meth:`AttrSortOrder.compare` reference (``None`` sorts first, reversed for
descending). The keyset predicate therefore needs no NULL special-casing beyond
what that fixed order implies.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.util.sort_order import AttrSortOrder, SortOrder

# A Mongo sort spec: a list of ``(field, 1|-1)`` pairs, as ``motor`` expects.
SortSpec = list[tuple[str, int]]

# A sort handler: ``(ctx, node) -> a single ``(field, direction)`` pair``.
SortHandler = Callable[["MongoSortContext", SortOrder[Any]], tuple[str, int]]


def _attr_order(ctx: MongoSortContext, node: SortOrder[Any]) -> tuple[str, int]:
    """The ``(field, direction)`` for one attribute (``node`` is an ``AttrSortOrder``)."""
    field = ctx.field_for(node.attribute)  # type: ignore[attr-defined]
    return field, -1 if node.descending else 1  # type: ignore[attr-defined]


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
            f"MongoSortConverter registry is incomplete: {sorted(t.__name__ for t in missing)}"
        )


_assert_registry_is_complete()


@dataclass(frozen=True)
class MongoSortContext:
    """Everything a sort handler needs, passed as one frozen object.

    Args:
        fields: DTO attribute name -> the Mongo field it maps to. Only the
            resource's *sortable* fields are present, so a field projected away
            from the read model cannot be sorted on (``?sort=secret`` would leak
            the hidden value's relative order).
        id_field: The DTO's identifier field, whose Mongo field is ``_id``.
    """

    fields: Mapping[str, str]
    id_field: str = "id"

    def field_for(self, attribute: str) -> str:
        """The Mongo field for ``attribute``; raise :class:`InvalidInputError` if absent."""
        try:
            return self.fields[attribute]
        except KeyError:
            raise InvalidInputError(f"Unknown or non-sortable sort field {attribute!r}") from None


class MongoSortConverter:
    """Convert a standard :class:`SortOrder` into a Mongo sort spec."""

    def __init__(self, ctx: MongoSortContext) -> None:
        self.ctx = ctx

    def apply(self, sort_order: SortOrder[Any]) -> SortSpec:
        """The Mongo sort spec for ``sort_order``, with ``_id`` appended ascending."""
        try:
            handler = _REGISTRY[type(sort_order)]
        except KeyError:
            raise InvalidInputError(
                f"No Mongo conversion is registered for {type(sort_order).__name__}"
            ) from None
        return [handler(self.ctx, sort_order), (self.ctx.field_for(self.ctx.id_field), 1)]
