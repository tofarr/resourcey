"""Translate a :class:`SearchFilter` tree into a Mongo query (issue #80).

The Mongo twin of :mod:`resourcey.sql.filter_converter`: same rule-list shape,
same explicit ordering, same "unconvertible raises" policy. Only the produced
value differs — a query ``dict`` (``None`` = no restriction) instead of a
SQLAlchemy condition.

An attribute resolves to a *field name* (the identifier maps to ``_id``), and
each value leaf produces a self-contained ``{field: condition}`` fragment.
Logical nodes compose fragments under ``$and`` / ``$or``.

Negation is the exact complement
--------------------------------
Mongo's ``$ne`` is *not* the in-memory ``matches`` complement for a non-None
value: ``{f: {"$ne": v}}`` also matches documents where ``f`` is absent or null,
but ``EqFilter.matches`` is already false for those, so the complement is true.
Each value rule therefore supplies its own complement, which is the query-level
``$nor`` of the positive fragment.

An unconvertible node raises :class:`~resourcey.core.errors.UnsupportedFilterError`
unless the resource set ``allow_filter_iteration``.

This module imports no code outside the framework.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from resourcey.core.errors import UnsupportedFilterError
from resourcey.util.search_filter import (
    AllFilter,
    AndFilter,
    AttrFilter,
    BaseObjectFilter,
    ContainsFilter,
    EqFilter,
    GeFilter,
    GtFilter,
    InFilter,
    LeFilter,
    LtFilter,
    NoMatchFilter,
    NotFilter,
    OrFilter,
    SearchFilter,
)

# A query fragment: a Mongo query dict, or ``None`` for "no restriction".
Query = dict[str, Any] | None

# A value-leaf handler: ``(field, value) -> query``.
ValueHandler = Callable[[str, Any], Query]

# A query that matches no document. ``{"$nor": [{}]}`` matches nothing
# unconditionally, including a document whose ``_id`` is itself null — which
# ``{"_id": None}`` would wrongly match.
_MATCH_NONE: Query = {"$nor": [{}]}


def _encode(value: Any) -> Any:
    """Encode a value for a Mongo query (``UUID`` -> string, datetimes to UTC).

    Filter values must be encoded the same way the documents store them.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


def _escaped(value: Any) -> str:
    """Escape regex metacharacters so a substring matches literally."""
    return re.escape(str(value))


def _negated_of(positive: ValueHandler) -> ValueHandler:
    """The exact complement of ``positive`` — its query-level ``$nor``."""

    def negated(field: str, value: Any) -> Query:
        inner = positive(field, value)
        if inner is None:  # pragma: no cover - operators never return None
            return None
        return {"$nor": [inner]}

    return negated


# ---------------------------------------------------------------------------
# Value operators
# ---------------------------------------------------------------------------


def _eq_positive(field: str, value: Any) -> Query:
    return {field: _encode(value)}


def _gt_positive(field: str, value: Any) -> Query:
    return {field: {"$gt": _encode(value)}}


def _ge_positive(field: str, value: Any) -> Query:
    return {field: {"$gte": _encode(value)}}


def _lt_positive(field: str, value: Any) -> Query:
    return {field: {"$lt": _encode(value)}}


def _le_positive(field: str, value: Any) -> Query:
    return {field: {"$lte": _encode(value)}}


def _contains_positive(field: str, value: Any) -> Query:
    return {field: {"$regex": _escaped(value), "$options": "i"}}


def _in_positive(field: str, value: Any) -> Query:
    # An empty set matches nothing (fail-closed); ``$in: []`` already matches no
    # document, which is exactly the deny we want.
    return {field: {"$in": [_encode(item) for item in value]}}


@dataclass(frozen=True)
class ValueOperator:
    """The two polarities of one value leaf."""

    positive: ValueHandler
    negated: ValueHandler


STANDARD_VALUE_OPERATORS: Mapping[type[SearchFilter[Any]], ValueOperator] = {
    EqFilter: ValueOperator(_eq_positive, _negated_of(_eq_positive)),
    GtFilter: ValueOperator(_gt_positive, _negated_of(_gt_positive)),
    GeFilter: ValueOperator(_ge_positive, _negated_of(_ge_positive)),
    LtFilter: ValueOperator(_lt_positive, _negated_of(_lt_positive)),
    LeFilter: ValueOperator(_le_positive, _negated_of(_le_positive)),
    ContainsFilter: ValueOperator(_contains_positive, _negated_of(_contains_positive)),
    InFilter: ValueOperator(_in_positive, _negated_of(_in_positive)),
}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class MongoFilterRule(ABC):
    """One conversion strategy for one (or more) filter node types."""

    @abstractmethod
    def handles(self, node: SearchFilter[Any]) -> bool:
        """Whether this rule converts ``node``."""

    @abstractmethod
    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        """The query for ``node`` — its complement when ``negated``."""


def _narrow(node: SearchFilter[Any], node_type: type[Any]) -> Any:
    """``node`` typed as ``node_type``, after ``handles`` already proved the type."""
    if not isinstance(node, node_type):  # pragma: no cover - handles() gate
        raise UnsupportedFilterError(f"{type(node).__name__} is not a {node_type.__name__}")
    return node


@dataclass(frozen=True)
class AllRule(MongoFilterRule):
    """``AllFilter`` — no restriction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AllFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        return _MATCH_NONE if negated else None


@dataclass(frozen=True)
class NoMatchRule(MongoFilterRule):
    """``NoMatchFilter`` — matches nothing."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, NoMatchFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        return None if negated else _MATCH_NONE


@dataclass(frozen=True)
class NotRule(MongoFilterRule):
    """``NotFilter`` — flips the polarity of its child."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, NotFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        inner = _narrow(node, NotFilter)
        return await converter.condition(inner.filter, field_name=field_name, negated=not negated)


@dataclass(frozen=True)
class AndRule(MongoFilterRule):
    """``AndFilter`` — conjunction; under negation, De Morgan to a disjunction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AndFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        filters = _narrow(node, AndFilter).filters
        children = await converter.conditions(filters, field_name=field_name, negated=negated)
        if negated:
            # An empty conjunction is TRUE, so its complement matches nothing.
            return _disjunction(children) if children else _MATCH_NONE
        return _conjunction(children)


@dataclass(frozen=True)
class OrRule(MongoFilterRule):
    """``OrFilter`` — disjunction; under negation, De Morgan to a conjunction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, OrFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        filters = _narrow(node, OrFilter).filters
        children = await converter.conditions(filters, field_name=field_name, negated=negated)
        if negated:
            # An empty disjunction is FALSE, so its complement is no restriction.
            return _conjunction(children) if children else None
        return _disjunction(children)


@dataclass(frozen=True)
class ObjectFilterRule(MongoFilterRule):
    """``BaseObjectFilter`` — lower its ``<attribute>__<op>`` fields, then convert."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, BaseObjectFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        standard = _narrow(node, BaseObjectFilter).to_standard()
        return await converter.condition(standard, field_name=field_name, negated=negated)


@dataclass(frozen=True)
class AttrRule(MongoFilterRule):
    """``AttrFilter`` — resolve the attribute to a *queryable* field, then convert."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AttrFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        attribute_filter = _narrow(node, AttrFilter)
        bound = converter.ctx.field_for(attribute_filter.attribute)
        return await converter.condition(attribute_filter.filter, field_name=bound, negated=negated)


@dataclass(frozen=True)
class ValueRule(MongoFilterRule):
    """The value leaves; ``operators`` is the extension point for an app's own leaf."""

    operators: Mapping[type[SearchFilter[Any]], ValueOperator] = field(
        default_factory=lambda: dict(STANDARD_VALUE_OPERATORS)
    )

    def handles(self, node: SearchFilter[Any]) -> bool:
        return type(node) in self.operators

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: MongoFilterConverter,
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        if field_name is None:
            raise UnsupportedFilterError(
                f"{type(node).__name__} reached the converter without a bound field; "
                "a value filter must sit under an AttrFilter"
            )
        operator = self.operators[type(node)]
        handler = operator.negated if negated else operator.positive
        return handler(field_name, cast("Any", node).value)


def _conjunction(children: Sequence[Query]) -> Query:
    """``$and`` of the children; ``None`` is TRUE, so it is dropped (identity)."""
    present = [child for child in children if child is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return {"$and": present}


def _disjunction(children: Sequence[Query]) -> Query:
    """``$or`` of the children; ``None`` is TRUE, so it absorbs the whole query."""
    if any(child is None for child in children):
        return None
    if not children:
        return _MATCH_NONE
    if len(children) == 1:
        return children[0]
    return {"$or": list(children)}


DEFAULT_RULES: tuple[MongoFilterRule, ...] = (
    AllRule(),
    NoMatchRule(),
    NotRule(),
    AndRule(),
    OrRule(),
    ObjectFilterRule(),
    AttrRule(),
    ValueRule(),
)


# ---------------------------------------------------------------------------
# Context + converter
# ---------------------------------------------------------------------------


@dataclass
class MongoFilterContext:
    """Everything a rule needs, passed as one object.

    ``fields`` holds only the resource's *queryable* fields, so a field projected
    away from the read model cannot be filtered. ``meta`` is a mutable cache
    shared by every rule of one conversion.
    """

    fields: Mapping[str, str]
    id_field: str = "id"
    meta: MutableMapping[Any, Any] = field(default_factory=dict)

    def field_for(self, attribute: str) -> str:
        """The Mongo field for ``attribute``; raise :class:`UnsupportedFilterError` if absent."""
        try:
            return self.fields[attribute]
        except KeyError:
            raise UnsupportedFilterError(
                f"Attribute {attribute!r} is not a queryable field of this resource"
            ) from None


class MongoFilterConverter:
    """Convert a filter tree into a Mongo query through an ordered rule list."""

    def __init__(
        self, ctx: MongoFilterContext, rules: Sequence[MongoFilterRule] = DEFAULT_RULES
    ) -> None:
        self.ctx = ctx
        self.rules = tuple(rules)

    async def apply(self, filter_: SearchFilter[Any]) -> Query:
        """The Mongo query for ``filter_`` (``None`` when it imposes no restriction)."""
        return await self.condition(filter_)

    async def condition(
        self,
        node: SearchFilter[Any],
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> Query:
        """The query for ``node``, or its exact complement when ``negated``."""
        for rule in self.rules:
            if rule.handles(node):
                return await rule.convert(node, self, field_name=field_name, negated=negated)
        raise UnsupportedFilterError(f"No Mongo conversion is registered for {type(node).__name__}")

    async def conditions(
        self,
        nodes: Sequence[SearchFilter[Any]],
        *,
        field_name: str | None = None,
        negated: bool = False,
    ) -> list[Query]:
        """Convert children **sequentially** — a rule may share IO."""
        return [
            await self.condition(node, field_name=field_name, negated=negated) for node in nodes
        ]
