"""Translate a standard :class:`SearchFilter` tree into SQLAlchemy conditions (issue #79).

This is the only ``v2`` file that imports SQLAlchemy for filtering. It consumes
only the *standard* filter tree (:mod:`resourcey.v2.util.search_filter`); an
object filter has already lowered itself, so a custom filter needs no handler
here — it lowers to standard nodes and is converted like any other.

Three registries, not one
-------------------------
Dispatch is split by what each node needs, which keeps the dispatcher trivial
and makes a structural mistake detectable:

* **Logical** — ``AllFilter``, ``NoMatchFilter``, ``AndFilter``, ``OrFilter``,
  ``NotFilter``. Converted with no column; they recurse.
* **Attribute** — ``AttrFilter``. Its one job is to resolve an attribute name to
  a column and bind it for the subtree below.
* **Operator** — ``EqFilter`` / ``GtFilter`` / ``GeFilter`` / ``LtFilter`` /
  ``LeFilter`` / ``ContainsFilter``. Only reachable *with a bound column*; the
  handler signature is ``(ctx, column, value) -> ColumnElement[bool]``. A value
  leaf reached with no column is a detected structural impossibility, not a
  per-handler bug.

Registration is keyed on the filter type (``register_operator`` /
``register_logical``). Because the node set is a discriminated union, the
registries are enumerable, and the module asserts at import time that every
standard leaf has a handler — the same trick the manifest uses to catch a
mistyped ``Action``.

NULL-safe negation
------------------
SQL ``NOT (col = 'x')`` compiles to ``col != 'x'``, which is *unknown* for a
NULL row, so a NULL row matches neither ``EqFilter('x')`` nor its negation and
silently vanishes from both branches. Every operator therefore registers a
``(positive, negated)`` pair; the negated form is NULL-safe (``IS DISTINCT
FROM`` for equality, ``col IS NULL OR <complement>`` for the rest), so it agrees
with the in-memory :meth:`SearchFilter.matches` complement on every row.

Purity / IO split
-----------------
:class:`SqlFilterConverter` splits into ``resolve()`` (the only place a handler
may do IO; empty today) and ``apply()`` (pure statement building). The common
path is a pure, trivially testable builder.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import Column, ColumnElement, and_, false, not_, or_
from sqlalchemy.ext.asyncio import AsyncSession

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

S = TypeVar("S")

# A converter handler for a logical node: ``(converter, node) -> condition | None``.
LogicalHandler = Callable[["SqlFilterConverter", SearchFilter[Any]], "ColumnElement[bool] | None"]
# A converter handler for a value leaf: ``(ctx, column, value) -> condition``.
OperatorHandler = Callable[["SqlFilterContext", Column[Any], Any], ColumnElement[bool]]

# ``condition | None``: ``None`` means "no restriction" (matches every row), the
# same convention ``v1`` used so ``AllFilter`` and an unset object filter are
# uniform.


def _naive(value: Any) -> Any:
    """Normalise an aware datetime to UTC before binding it to a column.

    All ORM timestamp columns use ``DateTime(timezone=True)`` (timestamptz).
    Comparing an aware datetime with a non-UTC offset against such a column
    raises an asyncpg offset mismatch that is not obvious from the call site.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


# ---------------------------------------------------------------------------
# Value-operator handlers
# ---------------------------------------------------------------------------


def _eq_positive(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    if value is None:
        return column.is_(None)
    return cast("ColumnElement[bool]", column == _naive(value))


def _eq_negated(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    if value is None:
        return column.is_not(None)
    # IS DISTINCT FROM is NULL-safe: a NULL row lands in the negated branch
    # instead of vanishing (naive ``!=`` would drop it).
    return column.is_distinct_from(_naive(value))


def _gt_positive(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column > _naive(value))


def _gt_negated(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column <= _naive(value))


def _ge_positive(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column >= _naive(value))


def _ge_negated(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column < _naive(value))


def _lt_positive(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column < _naive(value))


def _lt_negated(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column >= _naive(value))


def _le_positive(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column <= _naive(value))


def _le_negated(ctx: SqlFilterContext, column: Column[Any], value: Any) -> ColumnElement[bool]:
    return or_(column.is_(None), column > _naive(value))


def _contains_pattern(value: Any) -> str:
    # Strip LIKE wildcards from user input so ``%`` / ``_`` match literally.
    safe = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{safe}%"


def _contains_positive(
    ctx: SqlFilterContext, column: Column[Any], value: Any
) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", column.ilike(_contains_pattern(value), escape="\\"))


def _contains_negated(
    ctx: SqlFilterContext, column: Column[Any], value: Any
) -> ColumnElement[bool]:
    # A NULL column does not contain the needle, so it belongs in the negation.
    return or_(column.is_(None), not_(column.ilike(_contains_pattern(value), escape="\\")))


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
    """Register a logical node handler pair (positive + NULL-safe negated)."""
    _LOGICAL_REGISTRY[filter_type] = (positive, negated)


def register_operator(
    filter_type: type[SearchFilter[Any]],
    positive: OperatorHandler,
    negated: OperatorHandler,
) -> None:
    """Register a value-leaf handler pair (positive + NULL-safe negated)."""
    _OPERATOR_REGISTRY[filter_type] = (positive, negated)


def is_operator(node: SearchFilter[Any]) -> bool:
    """Whether ``node`` is a value leaf (needs a bound column)."""
    return type(node) in _OPERATOR_REGISTRY


# -- logical handlers -------------------------------------------------------


def _all_positive(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    return None  # no restriction: matches every row


def _all_negated(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    return false()  # its complement matches nothing


def _no_match_positive(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    return false()


def _no_match_negated(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    return None  # the complement of "match nothing" matches everything


def _and_positive(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    conditions = [c for c in converter._child_conditions(node) if c is not None]
    if not conditions:
        return None
    return and_(*conditions) if len(conditions) > 1 else conditions[0]


def _and_negated(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    # NOT (a AND b) == (NOT a) OR (NOT b)
    conditions = [c for c in converter._child_negated(node) if c is not None]
    if not conditions:
        # An empty conjunction matches every row, so its complement matches none.
        return false()
    return or_(*conditions) if len(conditions) > 1 else conditions[0]


def _or_positive(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    children = converter._child_conditions(node)
    # Any child with no restriction makes the whole disjunction unrestricted.
    if any(c is None for c in children):
        return None
    present = [c for c in children if c is not None]
    if not present:
        return false()
    return or_(*present) if len(present) > 1 else present[0]


def _or_negated(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    # NOT (a OR b) == (NOT a) AND (NOT b)
    children = converter._child_negated(node)
    if any(c is None for c in children):
        return None
    present = [c for c in children if c is not None]
    if not present:
        # An empty disjunction matches no row, so its complement matches every row.
        return None
    return and_(*present) if len(present) > 1 else present[0]


def _not_positive(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    child = converter._resolve_not_child(node)
    return converter.negated_condition(child)


def _not_negated(
    converter: SqlFilterConverter, node: SearchFilter[Any]
) -> ColumnElement[bool] | None:
    child = converter._resolve_not_child(node)
    return converter.condition(child)


register_logical(AllFilter, _all_positive, _all_negated)
register_logical(NoMatchFilter, _no_match_positive, _no_match_negated)
register_logical(AndFilter, _and_positive, _and_negated)
register_logical(OrFilter, _or_positive, _or_negated)
register_logical(NotFilter, _not_positive, _not_negated)
register_operator(EqFilter, _eq_positive, _eq_negated)
register_operator(GtFilter, _gt_positive, _gt_negated)
register_operator(GeFilter, _ge_positive, _ge_negated)
register_operator(LtFilter, _lt_positive, _lt_negated)
register_operator(LeFilter, _le_positive, _le_negated)
register_operator(ContainsFilter, _contains_positive, _contains_negated)


def _assert_registries_are_complete() -> None:
    """Fail at import time if a standard node has no handler.

    The registries are the single place a standard node becomes SQL, so a new
    leaf added without a handler is caught here rather than at request time.
    """
    standard_logical = {AllFilter, NoMatchFilter, AndFilter, OrFilter, NotFilter}
    standard_operators = {EqFilter, GtFilter, GeFilter, LtFilter, LeFilter, ContainsFilter}
    missing_logical = standard_logical - set(_LOGICAL_REGISTRY)
    missing_operators = standard_operators - set(_OPERATOR_REGISTRY)
    if missing_logical or missing_operators:
        raise RuntimeError(
            "SqlFilterConverter registries are incomplete: "
            f"logical={sorted(t.__name__ for t in missing_logical)}, "
            f"operators={sorted(t.__name__ for t in missing_operators)}"
        )


_assert_registries_are_complete()


# ---------------------------------------------------------------------------
# Context + converter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlFilterContext:
    """Everything a conversion handler needs, passed as one frozen object.

    Args:
        columns: DTO attribute name -> the table column it maps to. Only the
            resource's *queryable* fields are present, so a field projected away
            from the read model cannot be filtered (``?secret__eq=`` discloses a
            value the outside world never sees).
        session: The live session, available to handlers that need more than one
            statement (empty today, but it keeps handler signatures stable).
        allow_iteration: Whether an unconvertible filter may fall back to an
            in-memory scan. Off by default: a silent full-table scan behind a
            public ``GET`` is a DoS and voids the keyset-pagination guarantee.
    """

    columns: Mapping[str, Column[Any]]
    session: AsyncSession | None = None
    allow_iteration: bool = False

    def column_for(self, attribute: str) -> Column[Any]:
        """The column for ``attribute``; raise :class:`UnsupportedFilterError` if absent."""
        try:
            return self.columns[attribute]
        except KeyError:
            raise UnsupportedFilterError(
                f"Attribute {attribute!r} is not a queryable field of this resource"
            ) from None


class SqlFilterConverter:
    """Convert a standard filter tree into SQLAlchemy conditions.

    ``resolve()`` is the only phase that may do IO (no handler needs it today);
    ``apply()`` is pure statement building over the resolved context.
    """

    def __init__(self, ctx: SqlFilterContext) -> None:
        self.ctx = ctx

    async def resolve(self) -> None:
        """Run any IO-needing handlers and cache their results onto ``ctx``.

        Empty today: every standard handler is pure. It exists so a future
        relationship-traversal handler can query without changing signatures.
        """
        return None

    def apply(self, stmt: S, filter_: SearchFilter[Any]) -> S:
        """Return ``stmt`` with ``filter_``'s WHERE clause applied (if any)."""
        condition = self.condition(filter_)
        if condition is None:
            return stmt
        return cast("S", stmt.where(condition))  # type: ignore[attr-defined]

    def condition(
        self, node: SearchFilter[Any], column: Column[Any] | None = None
    ) -> ColumnElement[bool] | None:
        """The positive condition for ``node`` (``None`` = no restriction)."""
        if isinstance(node, AttrFilter):
            return self.condition(node.filter, self.ctx.column_for(node.attribute))
        if is_operator(node):
            if column is None:
                raise UnsupportedFilterError(
                    f"{type(node).__name__} reached the converter without a bound column; "
                    "a value filter must sit under an AttrFilter"
                )
            handler, _ = _OPERATOR_REGISTRY[type(node)]
            return handler(self.ctx, column, node.value)  # type: ignore[attr-defined]
        try:
            positive, _ = _LOGICAL_REGISTRY[type(node)]
        except KeyError:
            raise UnsupportedFilterError(
                f"No SQL conversion is registered for {type(node).__name__}"
            ) from None
        return positive(self, node)

    def negated_condition(
        self, node: SearchFilter[Any], column: Column[Any] | None = None
    ) -> ColumnElement[bool] | None:
        """The NULL-safe complement of :meth:`condition` (``None`` = matches all)."""
        if isinstance(node, AttrFilter):
            return self.negated_condition(node.filter, self.ctx.column_for(node.attribute))
        if is_operator(node):
            if column is None:
                raise UnsupportedFilterError(
                    f"{type(node).__name__} reached the converter without a bound column; "
                    "a value filter must sit under an AttrFilter"
                )
            _, negated = _OPERATOR_REGISTRY[type(node)]
            return negated(self.ctx, column, node.value)  # type: ignore[attr-defined]
        try:
            _, negated_logical = _LOGICAL_REGISTRY[type(node)]
        except KeyError:
            raise UnsupportedFilterError(
                f"No SQL conversion is registered for {type(node).__name__}"
            ) from None
        return negated_logical(self, node)

    # -- child helpers (used by the logical handlers) -------------------

    def _child_conditions(self, node: SearchFilter[Any]) -> list[ColumnElement[bool] | None]:
        return [self.condition(child) for child in node.filters]  # type: ignore[attr-defined]

    def _child_negated(self, node: SearchFilter[Any]) -> list[ColumnElement[bool] | None]:
        return [self.negated_condition(child) for child in node.filters]  # type: ignore[attr-defined]

    def _resolve_not_child(self, node: SearchFilter[Any]) -> SearchFilter[Any]:
        return node.filter  # type: ignore[attr-defined, no-any-return]
