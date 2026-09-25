"""Translate a standard :class:`SearchFilter` tree into a Mongo query (issue #80).

Mirrors :mod:`resourcey.v2.sql.filter_converter`: it consumes only the *standard*
filter tree (an object filter lowers itself first), dispatches through **three
registries** (logical / attribute / operator) with an import-time completeness
assert, and registers a ``(positive, negated)`` pair per operator. Because the
node set is a discriminated union the registries are enumerable, so a new leaf
added without a handler fails at import rather than at request time.

A Mongo query is a plain ``dict`` (``None`` = no restriction, matching every
document). Unlike the SQL converter there is no column object: an attribute
resolves to a *field name* (the identifier maps to Mongo's ``_id``), and each
value leaf produces a self-contained ``{field: condition}`` fragment. Logical
nodes compose fragments under ``$and`` / ``$or`` / ``$nor``.

Negation is the exact complement
--------------------------------
The in-memory :meth:`SearchFilter.matches` complement is the contract. Mongo's
``$ne`` is *not* that complement for a non-None value: ``{f: {"$ne": v}}`` also
matches documents where ``f`` is absent or null, but a ``NotFilter`` around an
``EqFilter`` should match exactly the documents where ``matches`` is false — and
for an absent / null field ``EqFilter.matches`` is already false, so the
complement is true. The negated form is therefore the query-level ``$nor`` of the
positive fragment, which matches *every* document the positive fragment does not
— absent and null rows included. A test pins that the negated form and
``matches``' complement agree on an absent / null field.

All-or-nothing pushdown: an unconvertible node raises
:class:`~resourcey.v2.core.errors.UnsupportedFilterError` unless the resource set
``allow_filter_iteration`` (the same unbounded-scan guard the SQL backend uses).

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from resourcey.v2.core.errors import UnsupportedFilterError
from resourcey.v2.util.search_filter import (
    AllFilter,
    AndFilter,
    AttrFilter,
    ContainsFilter,
    EqFilter,
    GeFilter,
    GtFilter,
    LeFilter,
    LtFilter,
    NoMatchFilter,
    NotFilter,
    OrFilter,
    SearchFilter,
)

# A query fragment: a Mongo query dict, or ``None`` for "no restriction".
Query = dict[str, Any] | None

LogicalHandler = Callable[["MongoFilterConverter", SearchFilter[Any]], Query]
OperatorHandler = Callable[["MongoFilterContext", str, Any], Query]

# A query that matches no document: ``_id`` is always present and non-null, so a
# query for a null ``_id`` never matches. Mongo has no dedicated "false".
_MATCH_NONE: Query = {"_id": None}


def _encode(value: Any) -> Any:
    """Encode a value for a Mongo query (``UUID`` -> string, datetimes to UTC).

    Documents store ``UUID`` as its string form (bson cannot encode a native
    ``UUID`` without a configured representation), so a filter value must be
    encoded the same way. An aware datetime is normalised to UTC for parity with
    the stored value.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


def _escaped(value: Any) -> str:
    """Escape regex metacharacters so a substring matches literally."""
    return re.escape(str(value))


def _negated_of(positive: OperatorHandler) -> OperatorHandler:
    """The exact complement of ``positive`` — its query-level ``$nor``."""

    def negated(ctx: MongoFilterContext, field: str, value: Any) -> Query:
        inner = positive(ctx, field, value)
        if inner is None:  # pragma: no cover - operators never return None
            return None
        return {"$nor": [inner]}

    return negated


# ---------------------------------------------------------------------------
# Value-operator handlers
# ---------------------------------------------------------------------------


def _eq_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: _encode(value)}


def _gt_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: {"$gt": _encode(value)}}


def _ge_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: {"$gte": _encode(value)}}


def _lt_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: {"$lt": _encode(value)}}


def _le_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: {"$lte": _encode(value)}}


def _contains_positive(ctx: MongoFilterContext, field: str, value: Any) -> Query:
    return {field: {"$regex": _escaped(value), "$options": "i"}}


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

_LOGICAL_REGISTRY: dict[type[SearchFilter[Any]], tuple[LogicalHandler, LogicalHandler]] = {}
_OPERATOR_REGISTRY: dict[type[SearchFilter[Any]], tuple[OperatorHandler, OperatorHandler]] = {}


def register_logical(
    filter_type: type[SearchFilter[Any]],
    positive: LogicalHandler,
    negated: LogicalHandler,
) -> None:
    """Register a logical node handler pair (positive + exact-complement negated)."""
    _LOGICAL_REGISTRY[filter_type] = (positive, negated)


def register_operator(
    filter_type: type[SearchFilter[Any]],
    positive: OperatorHandler,
    negated: OperatorHandler,
) -> None:
    """Register a value-leaf handler pair (positive + exact-complement negated)."""
    _OPERATOR_REGISTRY[filter_type] = (positive, negated)


def is_operator(node: SearchFilter[Any]) -> bool:
    """Whether ``node`` is a value leaf (needs a bound field)."""
    return type(node) in _OPERATOR_REGISTRY


# -- logical handlers -------------------------------------------------------


def _all_positive(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return None  # no restriction: matches every document


def _all_negated(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return _MATCH_NONE


def _no_match_positive(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return _MATCH_NONE


def _no_match_negated(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return None  # the complement of "match nothing" matches everything


def _and_positive(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    children = [c for c in converter._child_conditions(node) if c is not None]
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return {"$and": children}


def _and_negated(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    # NOT (a AND b) == (NOT a) OR (NOT b)
    children = [c for c in converter._child_negated(node) if c is not None]
    if not children:
        # An empty conjunction matches every document, so its complement matches none.
        return _MATCH_NONE
    if len(children) == 1:
        return children[0]
    return {"$or": children}


def _or_positive(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    children = converter._child_conditions(node)
    # Any child with no restriction makes the whole disjunction unrestricted.
    if any(c is None for c in children):
        return None
    present = [c for c in children if c is not None]
    if not present:
        return _MATCH_NONE
    if len(present) == 1:
        return present[0]
    return {"$or": present}


def _or_negated(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    # NOT (a OR b) == (NOT a) AND (NOT b)
    children = converter._child_negated(node)
    if any(c is None for c in children):
        return None
    present = [c for c in children if c is not None]
    if not present:
        # An empty disjunction matches no document, so its complement matches every one.
        return None
    if len(present) == 1:
        return present[0]
    return {"$and": present}


def _not_positive(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return converter.negated_condition(node.filter)  # type: ignore[attr-defined]


def _not_negated(converter: MongoFilterConverter, node: SearchFilter[Any]) -> Query:
    return converter.condition(node.filter)  # type: ignore[attr-defined]


register_logical(AllFilter, _all_positive, _all_negated)
register_logical(NoMatchFilter, _no_match_positive, _no_match_negated)
register_logical(AndFilter, _and_positive, _and_negated)
register_logical(OrFilter, _or_positive, _or_negated)
register_logical(NotFilter, _not_positive, _not_negated)
register_operator(EqFilter, _eq_positive, _negated_of(_eq_positive))
register_operator(GtFilter, _gt_positive, _negated_of(_gt_positive))
register_operator(GeFilter, _ge_positive, _negated_of(_ge_positive))
register_operator(LtFilter, _lt_positive, _negated_of(_lt_positive))
register_operator(LeFilter, _le_positive, _negated_of(_le_positive))
register_operator(ContainsFilter, _contains_positive, _negated_of(_contains_positive))


def _assert_registries_are_complete() -> None:
    """Fail at import time if a standard node has no handler."""
    standard_logical = {AllFilter, NoMatchFilter, AndFilter, OrFilter, NotFilter}
    standard_operators = {EqFilter, GtFilter, GeFilter, LtFilter, LeFilter, ContainsFilter}
    missing_logical = standard_logical - set(_LOGICAL_REGISTRY)
    missing_operators = standard_operators - set(_OPERATOR_REGISTRY)
    if missing_logical or missing_operators:
        raise RuntimeError(
            "MongoFilterConverter registries are incomplete: "
            f"logical={sorted(t.__name__ for t in missing_logical)}, "
            f"operators={sorted(t.__name__ for t in missing_operators)}"
        )


_assert_registries_are_complete()


# ---------------------------------------------------------------------------
# Context + converter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MongoFilterContext:
    """Everything a conversion handler needs, passed as one frozen object.

    Args:
        fields: DTO attribute name -> the Mongo field it maps to. Only the
            resource's *queryable* fields are present, so a field projected away
            from the read model cannot be filtered (``?secret__eq=`` discloses a
            value the outside world never sees).
        id_field: The DTO's identifier field, whose Mongo field is ``_id``.
    """

    fields: Mapping[str, str]
    id_field: str = "id"

    def field_for(self, attribute: str) -> str:
        """The Mongo field for ``attribute``; raise :class:`UnsupportedFilterError` if absent."""
        try:
            return self.fields[attribute]
        except KeyError:
            raise UnsupportedFilterError(
                f"Attribute {attribute!r} is not a queryable field of this resource"
            ) from None


class MongoFilterConverter:
    """Convert a standard filter tree into a Mongo query.

    ``resolve()`` is the only phase that may do IO (no handler needs it today);
    ``condition`` / ``apply`` are pure query building over the resolved context.
    """

    def __init__(self, ctx: MongoFilterContext) -> None:
        self.ctx = ctx

    async def resolve(self) -> None:
        """Run any IO-needing handlers and cache their results onto ``ctx``.

        Empty today: every standard handler is pure. It exists so a future
        relationship-traversal handler can query without changing signatures.
        """
        return None

    def apply(self, filter_: SearchFilter[Any]) -> Query:
        """The Mongo query for ``filter_`` (``None`` when it imposes no restriction)."""
        return self.condition(filter_)

    def condition(self, node: SearchFilter[Any], field: str | None = None) -> Query:
        """The positive query for ``node`` (``None`` = no restriction)."""
        if isinstance(node, AttrFilter):
            return self.condition(node.filter, self.ctx.field_for(node.attribute))
        if is_operator(node):
            if field is None:
                raise UnsupportedFilterError(
                    f"{type(node).__name__} reached the converter without a bound field; "
                    "a value filter must sit under an AttrFilter"
                )
            handler, _ = _OPERATOR_REGISTRY[type(node)]
            return handler(self.ctx, field, node.value)  # type: ignore[attr-defined]
        try:
            positive, _ = _LOGICAL_REGISTRY[type(node)]
        except KeyError:
            raise UnsupportedFilterError(
                f"No Mongo conversion is registered for {type(node).__name__}"
            ) from None
        return positive(self, node)

    def negated_condition(self, node: SearchFilter[Any], field: str | None = None) -> Query:
        """The exact complement of :meth:`condition` (``None`` = matches all)."""
        if isinstance(node, AttrFilter):
            return self.negated_condition(node.filter, self.ctx.field_for(node.attribute))
        if is_operator(node):
            if field is None:
                raise UnsupportedFilterError(
                    f"{type(node).__name__} reached the converter without a bound field; "
                    "a value filter must sit under an AttrFilter"
                )
            _, negated = _OPERATOR_REGISTRY[type(node)]
            return negated(self.ctx, field, node.value)  # type: ignore[attr-defined]
        try:
            _, negated_logical = _LOGICAL_REGISTRY[type(node)]
        except KeyError:
            raise UnsupportedFilterError(
                f"No Mongo conversion is registered for {type(node).__name__}"
            ) from None
        return negated_logical(self, node)

    # -- child helpers (used by the logical handlers) -------------------

    def _child_conditions(self, node: SearchFilter[Any]) -> list[Query]:
        return [self.condition(child) for child in node.filters]  # type: ignore[attr-defined]

    def _child_negated(self, node: SearchFilter[Any]) -> list[Query]:
        return [self.negated_condition(child) for child in node.filters]  # type: ignore[attr-defined]
