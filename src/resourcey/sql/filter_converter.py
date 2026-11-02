"""Translate a :class:`SearchFilter` tree into SQLAlchemy conditions (issue #79).

This is the only file that imports SQLAlchemy for filtering. Conversion is a
**rule list**, not a fixed dispatch table:

* A :class:`SqlFilterRule` is a frozen dataclass that answers ``handles(node)``
  and converts the node asynchronously. The default list
  (:data:`DEFAULT_RULES`) covers the standard tree; an app appends a rule for
  its own node type — a predicate over a related table (``EXISTS``), a tenant
  lookup, anything — without touching the framework.
* The list is **explicit and ordered**. There is no subclass scan and no
  process-global registry: ``SqlResource.get_filter_rules()`` is where a
  resource extends it, so two resources may disagree about a node type.
* An unconvertible node **raises** :class:`UnsupportedFilterError`. There is no
  "not applicable" boolean, because skipping a filter is fail-open.

Negation is chosen at the leaf
------------------------------
``condition(node, negated=True)`` asks for the *complement* of ``node``. The
logical rules push the flag down (De Morgan for ``And`` / ``Or``, flip for
``Not``) and each value rule supplies its own NULL-safe complement — for
``Gt`` that is ``col IS NULL OR col <= v``, not ``NOT (col > v)``, which would
drop NULL rows from both branches. The in-memory :meth:`SearchFilter.matches`
complement is the reference, pinned by ``specs/filtering.qnt`` laws 2 and 3.

Async, because a rule may need one query first
----------------------------------------------
``convert`` is a coroutine, so a rule that must resolve a set of ids before it
can build its criterion runs that query inline. ``ctx.meta`` is a mutable dict
shared by every rule of one conversion, so a rule may cache a result for a
sibling (or for its own second invocation under the other polarity). Children
are converted **sequentially** — a rule that touches ``ctx.session`` must not
race another rule on the same connection.

This module imports no code outside the framework.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import Column, ColumnElement, and_, false, not_, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import True_
from sqlalchemy.sql.schema import Table

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

S = TypeVar("S")

# A value-leaf handler: ``(column, value) -> condition``.
ValueHandler = Callable[[Column[Any], Any], ColumnElement[bool]]


def _naive(value: Any) -> Any:
    """Normalise an aware datetime to UTC before binding it to a column.

    Comparing an aware datetime with a non-UTC offset against a timestamptz
    column raises an asyncpg offset mismatch that is not obvious from the call
    site.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


def _naive_values(value: Any) -> list[Any]:
    """Normalise an ``InFilter``'s values for binding (aware datetimes -> UTC)."""
    return [_naive(item) for item in value]


# ---------------------------------------------------------------------------
# Value operators: a positive form and its NULL-safe complement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueOperator:
    """The two polarities of one value leaf.

    ``negated`` must agree with the in-memory complement on **every** row,
    including NULL. That is why it is a separate function rather than
    ``not_(positive)``.
    """

    positive: ValueHandler
    negated: ValueHandler


def _eq_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    if value is None:
        return column.is_(None)
    return cast("ColumnElement[bool]", column == _naive(value))


def _eq_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    if value is None:
        return column.is_not(None)
    # IS DISTINCT FROM is NULL-safe: a NULL row lands in the negated branch
    # instead of vanishing (naive ``!=`` would drop it).
    return column.is_distinct_from(_naive(value))


def _gt_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column > _naive(value))


def _gt_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column <= _naive(value))


def _ge_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column >= _naive(value))


def _ge_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column < _naive(value))


def _lt_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column < _naive(value))


def _lt_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column >= _naive(value))


def _le_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column <= _naive(value))


def _le_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column > _naive(value))


def _contains_pattern(value: Any) -> str:
    # Strip LIKE wildcards from user input so ``%`` / ``_`` match literally.
    safe = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{safe}%"


def _contains_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column.ilike(_contains_pattern(value), escape="\\"))


def _contains_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    # A NULL column does not contain the needle, so it belongs in the negation.
    return or_(column.is_(None), not_(column.ilike(_contains_pattern(value), escape="\\")))


def _in_positive(column: Column[Any], value: Any) -> ColumnElement[bool]:
    # An empty set matches nothing (fail-closed): return an explicit false rather
    # than compile ``col IN ()``.
    values = _naive_values(value)
    if not values:
        return false()
    return cast("ColumnElement[bool]", column.in_(values))


def _in_negated(column: Column[Any], value: Any) -> ColumnElement[bool]:
    # NULL-safe complement: a NULL column is not in the set, so it belongs in the
    # negated branch (naive ``NOT IN`` would drop it as Unknown). The complement of
    # an empty (matches-nothing) set is "matches everything".
    values = _naive_values(value)
    if not values:
        return true()
    return or_(column.is_(None), column.not_in(values))


STANDARD_VALUE_OPERATORS: Mapping[type[SearchFilter[Any]], ValueOperator] = {
    EqFilter: ValueOperator(_eq_positive, _eq_negated),
    GtFilter: ValueOperator(_gt_positive, _gt_negated),
    GeFilter: ValueOperator(_ge_positive, _ge_negated),
    LtFilter: ValueOperator(_lt_positive, _lt_negated),
    LeFilter: ValueOperator(_le_positive, _le_negated),
    ContainsFilter: ValueOperator(_contains_positive, _contains_negated),
    InFilter: ValueOperator(_in_positive, _in_negated),
}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class SqlFilterRule(ABC):
    """One conversion strategy for one (or more) filter node types.

    A rule is a frozen dataclass, not a Pydantic model: it is a stateless
    strategy that is never validated or serialized, so it has no fields unless
    it is parameterized.

    ``handles`` must be pure and cheap — a type test, or a type test plus a
    field test for a parameterized rule. It must **not** consult the database;
    anything needing IO belongs in :meth:`convert`.
    """

    @abstractmethod
    def handles(self, node: SearchFilter[Any]) -> bool:
        """Whether this rule converts ``node``."""

    @abstractmethod
    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        """The condition for ``node`` — its complement when ``negated``.

        ``column`` is set only when an enclosing :class:`AttrFilter` bound one.
        """


def _narrow(node: SearchFilter[Any], node_type: type[Any]) -> Any:
    """``node`` typed as ``node_type``, after ``handles`` already proved the type."""
    if not isinstance(node, node_type):  # pragma: no cover - handles() gate
        raise UnsupportedFilterError(f"{type(node).__name__} is not a {node_type.__name__}")
    return node


@dataclass(frozen=True)
class AllRule(SqlFilterRule):
    """``AllFilter`` — no restriction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AllFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        return false() if negated else true()


@dataclass(frozen=True)
class NoMatchRule(SqlFilterRule):
    """``NoMatchFilter`` — matches nothing."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, NoMatchFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        return true() if negated else false()


@dataclass(frozen=True)
class NotRule(SqlFilterRule):
    """``NotFilter`` — flips the polarity of its child."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, NotFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        return await converter.condition(
            _narrow(node, NotFilter).filter, column=column, negated=not negated
        )


@dataclass(frozen=True)
class AndRule(SqlFilterRule):
    """``AndFilter`` — conjunction; under negation, De Morgan to a disjunction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AndFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        filters = _narrow(node, AndFilter).filters
        children = await converter.conditions(filters, column=column, negated=negated)
        if negated:
            # An empty conjunction is TRUE, so its complement is FALSE.
            return or_(*children) if children else false()
        return and_(*children) if children else true()


@dataclass(frozen=True)
class OrRule(SqlFilterRule):
    """``OrFilter`` — disjunction; under negation, De Morgan to a conjunction."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, OrFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        filters = _narrow(node, OrFilter).filters
        children = await converter.conditions(filters, column=column, negated=negated)
        if negated:
            # An empty disjunction is FALSE, so its complement is TRUE.
            return and_(*children) if children else true()
        return or_(*children) if children else false()


@dataclass(frozen=True)
class ObjectFilterRule(SqlFilterRule):
    """``BaseObjectFilter`` — lower its ``<attribute>__<op>`` fields, then convert."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, BaseObjectFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        standard = _narrow(node, BaseObjectFilter).to_standard()
        return await converter.condition(standard, column=column, negated=negated)


@dataclass(frozen=True)
class AttrRule(SqlFilterRule):
    """``AttrFilter`` — resolve the attribute to a *queryable* column, then convert."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, AttrFilter)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        attribute_filter = _narrow(node, AttrFilter)
        bound = converter.ctx.column_for(attribute_filter.attribute)
        return await converter.condition(attribute_filter.filter, column=bound, negated=negated)


@dataclass(frozen=True)
class ValueRule(SqlFilterRule):
    """The value leaves — ``Eq`` / ``Gt`` / ``Ge`` / ``Lt`` / ``Le`` / ``Contains`` / ``In``.

    ``operators`` is the extension point for an app's own leaf type: pass
    ``{**STANDARD_VALUE_OPERATORS, MyLeaf: ValueOperator(pos, neg)}``.
    """

    operators: Mapping[type[SearchFilter[Any]], ValueOperator] = field(
        default_factory=lambda: dict(STANDARD_VALUE_OPERATORS)
    )

    def handles(self, node: SearchFilter[Any]) -> bool:
        return type(node) in self.operators

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        if column is None:
            raise UnsupportedFilterError(
                f"{type(node).__name__} reached the converter without a bound column; "
                "a value filter must sit under an AttrFilter"
            )
        operator = self.operators[type(node)]
        handler = operator.negated if negated else operator.positive
        return handler(column, cast("Any", node).value)


DEFAULT_RULES: tuple[SqlFilterRule, ...] = (
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
class SqlFilterContext:
    """Everything a rule needs, passed as one object.

    ``columns`` holds only the resource's *queryable* fields, so a field
    projected away from the read model cannot be filtered — that is the
    security gate, and it works by raising. ``table`` / ``id_column`` are for
    rules that build a correlated subquery (``EXISTS``) against this resource.
    ``meta`` is a mutable cache shared by every rule of one conversion, so a
    rule can hand a resolved value to a sibling (or to its own second
    invocation under the other polarity).
    """

    columns: Mapping[str, Column[Any]]
    session: AsyncSession | None = None
    table: Table | None = None
    id_column: Column[Any] | None = None
    meta: MutableMapping[Any, Any] = field(default_factory=dict)

    def column_for(self, attribute: str) -> Column[Any]:
        """The column for ``attribute``; raise :class:`UnsupportedFilterError` if absent."""
        try:
            return self.columns[attribute]
        except KeyError:
            raise UnsupportedFilterError(
                f"Attribute {attribute!r} is not a queryable field of this resource"
            ) from None


class SqlFilterConverter:
    """Convert a filter tree into SQLAlchemy conditions through an ordered rule list."""

    def __init__(
        self, ctx: SqlFilterContext, rules: Sequence[SqlFilterRule] = DEFAULT_RULES
    ) -> None:
        self.ctx = ctx
        self.rules = tuple(rules)

    async def apply(self, stmt: S, filter_: SearchFilter[Any]) -> S:
        """Return ``stmt`` with ``filter_``'s WHERE clause applied (if any)."""
        condition = await self.condition(filter_)
        if isinstance(condition, True_):
            # ``AllFilter`` imposes no restriction; a bare ``WHERE true`` is noise.
            return stmt
        return cast("S", stmt.where(condition))  # type: ignore[attr-defined]

    async def condition(
        self,
        node: SearchFilter[Any],
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> ColumnElement[bool]:
        """The condition for ``node``, or its NULL-safe complement when ``negated``."""
        for rule in self.rules:
            if rule.handles(node):
                return await rule.convert(node, self, column=column, negated=negated)
        raise UnsupportedFilterError(f"No SQL conversion is registered for {type(node).__name__}")

    async def conditions(
        self,
        nodes: Sequence[SearchFilter[Any]],
        *,
        column: Column[Any] | None = None,
        negated: bool = False,
    ) -> list[ColumnElement[bool]]:
        """Convert children **sequentially** — a rule may share ``ctx.session``."""
        return [await self.condition(node, column=column, negated=negated) for node in nodes]
