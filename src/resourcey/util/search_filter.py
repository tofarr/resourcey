"""Search filters: declarative, reusable predicates over a resource type.

A `SearchFilter` is a discriminated-union (``DiscriminatedUnionMixin``)
Pydantic model, generic over the entity type `T` it filters. It exposes two
views of the same predicate:

* `matches(item)` — an in-memory boolean check applied to a single entity
  instance. Used for permission-scoped row filtering and ad-hoc checks.
* `filter_sql(stmt)` — applies the predicate as SQLAlchemy WHERE clauses to a
  `Select` construct, so collection endpoints can push the filter into the DB
  query instead of materializing rows.

`BaseSearchFilter` is the convenience base for the common case where a filter
mirrors a resource's fields. A subclass declares optional fields named
`<attribute>__<op>` (e.g. `email__contains`, `created_at__gte`) and the base
reflects over its own Pydantic field declarations to build `matches` and
`filter_sql` automatically.

`AttributeFilter` provides a generic runtime-specified filter with an attribute
name, value, and condition. It is useful for permission scopes and dynamic
queries where the filterable attributes are not known at schema definition time.

Supported operators (the suffix after the final `__` for BaseSearchFilter, or
the `condition` field for AttributeFilter):

=========== =============================================================
suffix      semantics
=========== =============================================================
contains    substring / membership match (case-insensitive for `str`)
eq          equals
ne          not equals
lt          strictly less than
lte         less than or equal
gt          strictly greater than
gte         greater than or equal
in          membership in a list of accepted values
=========== =============================================================

Example::

    class UserSearchFilter(BaseSearchFilter[User]):
        email__contains: str | None = None
        created_at__gte: datetime | None = None

    f = UserSearchFilter(email__contains="alice")
    f.matches(user)                       # bool
    f.filter_sql(select(User))            # Select with WHERE applied

    # Generic attribute filter for dynamic queries:
    g = AttributeFilter(attribute="email", value="alice", condition=Condition.CONTAINS)
    g.matches(user)                       # bool

The same filter shapes are intended to be used as optional inclusions on
search endpoints and as the per-item scope of a permission grant.

Factory functions `and_filter()` and `or_filter()` normalize composite filters,
applying algebraic identities (e.g., And with None -> None, Or with All -> All)
and returning the simplest equivalent filter type.
"""

from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, TypeVar, cast

from pydantic import field_validator
from sqlalchemy import and_, false, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select

from resourcey.util.models import DiscriminatedUnionMixin

T = TypeVar("T")
S = TypeVar("S", bound=Select[Any])


class Condition(Enum):
    """Comparison operators for attribute filters."""

    CONTAINS = "contains"
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"


# A SQL boolean expression (the WHERE clause fragment a filter contributes).
# `None` means "no restriction" — the filter matches every row, so it adds no
# clause. This keeps `AllSearchFilter` and an unset `BaseSearchFilter` uniform:
# both contribute `None` and therefore no WHERE.
SqlCondition = ColumnElement[bool] | None

# Operators recognized on the `<attribute>__<op>` field-name convention.
# Each maps to (sql_builder, in_memory_predicate). The SQL builder receives
# the SQLAlchemy column and the filter value; the in-memory predicate receives
# the item's attribute value and the filter value.


def _naive(value: Any) -> Any:
    """Normalize datetimes for safe comparison against timestamptz columns.

    All ORM timestamp columns use ``DateTime(timezone=True)`` (timestamptz).
    Comparing an aware datetime with a non-UTC offset against such a column
    raises an asyncpg offset mismatch. Convert aware datetimes to UTC so the
    comparison is always correct regardless of the session timezone.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


def _sql_contains(column: Any, value: Any) -> Any:
    # ilike is case-insensitive substring match; for non-string columns this
    # still works but is rarely meaningful. Stripping stray wildcards from
    # user input prevents LIKE-injection of `%`/`_`.
    safe = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{safe}%", escape="\\")


def _in_mem_contains(attr_value: Any, value: Any) -> bool:
    if attr_value is None:
        return False
    if isinstance(attr_value, str) and isinstance(value, str):
        return value.lower() in attr_value.lower()
    # Fall back to membership for sequence-typed attributes.
    try:
        return value in attr_value
    except TypeError:
        return False


def _sql_in(column: Any, value: Any) -> Any:
    # ``value`` is the parsed list of accepted values. An empty list matches
    # nothing (consistent with SQL ``IN ()`` semantics, which SQLite/Postgres
    # reject syntactically -- ``false()`` expresses "match no row" portably).
    values = list(value) if not isinstance(value, (str, bytes)) else [value]
    if not values:
        return false()
    return column.in_(values)


def _in_mem_in(attr_value: Any, value: Any) -> bool:
    values = list(value) if not isinstance(value, (str, bytes)) else [value]
    if not values:
        return False
    return attr_value in values


_OPS: dict[str, tuple[Callable[[Any, Any], Any], Callable[[Any, Any], bool]]] = {
    "contains": (_sql_contains, _in_mem_contains),
    "eq": (lambda c, v: c == _naive(v), operator.eq),
    "ne": (lambda c, v: c != _naive(v), operator.ne),
    "lt": (lambda c, v: c < _naive(v), operator.lt),
    "lte": (lambda c, v: c <= _naive(v), operator.le),
    "gt": (lambda c, v: c > _naive(v), operator.gt),
    "gte": (lambda c, v: c >= _naive(v), operator.ge),
    "in": (_sql_in, _in_mem_in),
}

_SEPARATOR = "__"


# Generic[T] (not PEP 695 type params) is required so __class_getitem__ can
# capture the concrete entity type on the parameterized base class.
class SearchFilter(DiscriminatedUnionMixin, ABC, Generic[T]):
    """Abstract base for a filter over entity type `T`.

    Concrete subclasses participate in the discriminated-union machinery
    (a `kind` computed field tags the concrete filter type) so filter sets
    can be serialized and deserialized polymorphically.
    """

    # Resolved entity class for `BaseSearchFilter[T]` subclasses. Captured in
    # `__class_getitem__` (the parameterized base carries it) and inherited via
    # the MRO; never written from `__init_subclass__` so concrete subclasses
    # don't shadow the value with `None`.
    _entity_cls: type[Any] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def __class_getitem__(cls, item: Any) -> type[Any]:
        param = cast("type[Any]", super().__class_getitem__(item))
        arg = item[0] if isinstance(item, tuple) else item
        if isinstance(arg, type):
            param._entity_cls = arg
        return param

    @abstractmethod
    def matches(self, item: T) -> bool:
        """Whether *item* satisfies this filter."""
        raise NotImplementedError

    @abstractmethod
    def sql_condition(self) -> SqlCondition:
        """SQL boolean expression this filter contributes to a WHERE clause.

        Returning ``None`` means "no restriction" (matches every row); the
        default :meth:`filter_sql` adds no clause in that case. Composite
        filters combine their children's conditions here so a single
        ``WHERE`` is produced.
        """
        raise NotImplementedError

    def negated_sql_condition(self) -> SqlCondition:
        """SQL condition for the complement (items NOT matching this filter).

        Defaults to SQL ``NOT`` of :meth:`sql_condition`. Subclasses whose
        match predicate is NULL-sensitive (e.g. equality on a nullable column,
        where ``col != x`` is false for ``NULL``) override to produce a
        NULL-safe complement such as ``IS DISTINCT FROM`` so ``NULL`` rows
        fall into the complement rather than vanishing from both branches.

        Returning ``None`` means the complement imposes no restriction
        (matches every row) — e.g. when this filter itself matches nothing.
        """
        cond = self.sql_condition()
        if cond is None:
            # This filter matches every row, so its complement matches none.
            return false()
        return ~cond

    def filter_sql(self, stmt: S) -> S:
        """Return *stmt* with this filter's WHERE clause applied.

        Delegates to :meth:`sql_condition`; ``None`` (no restriction) leaves
        the statement untouched.
        """
        condition = self.sql_condition()
        if condition is None:
            return stmt
        return stmt.where(condition)


class BaseSearchFilter(SearchFilter[T]):
    """`SearchFilter` that derives its predicate from its own field names.

    A subclass declares optional fields named `<attr>__<op>` where `<op>` is
    one of: contains, eq, lt, lte, gt, gte. At call time the base reflects
    over the subclass's Pydantic fields, collects every non-`None` value, and
    applies the corresponding operator to the matching attribute of `T` — both
    in-memory (`matches`) and in SQL (`filter_sql`).

    The entity type `T` is resolved from the `Generic` parameterization (e.g.
    `BaseSearchFilter[User]`), so the SQLAlchemy column for an attribute is
    obtained via `getattr(entity_cls, attr)`.
    """

    def matches(self, item: T) -> bool:
        attr_value: Any
        for attr, op, value in self._active_clauses():
            _sql_builder, in_mem = _OPS[op]
            attr_value = getattr(item, attr, None)
            if not in_mem(attr_value, value):
                return False
        return True

    def sql_condition(self) -> SqlCondition:
        clauses: list[ColumnElement[bool]] = []
        for attr, op, value in self._active_clauses():
            sql_builder, _ = _OPS[op]
            column = self._column(attr)
            clauses.append(sql_builder(column, value))
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return and_(*clauses)

    def filter_sql(self, stmt: S) -> S:
        # Overridden only to surface a clear error for an unparameterized
        # entity; the common path delegates to sql_condition via the base.
        entity = type(self)._entity_cls
        if not isinstance(entity, type):
            raise TypeError(
                f"{type(self).__name__} is not parameterized with a concrete entity "
                "type; subclass as BaseSearchFilter[YourEntity] to enable SQL filtering."
            )
        return super().filter_sql(stmt)

    def _column(self, attr: str) -> Any:
        entity = type(self)._entity_cls
        if not isinstance(entity, type):
            raise TypeError(
                f"{type(self).__name__} is not parameterized with a concrete entity "
                "type; subclass as BaseSearchFilter[YourEntity] to enable SQL filtering."
            )
        column = getattr(entity, attr, None)
        if column is None:
            raise AttributeError(
                f"{entity.__name__!r} has no attribute {attr!r} "
                f"required by filter field {attr!r}__op"
            )
        return column

    def _active_clauses(self) -> Iterator[tuple[str, str, Any]]:
        """Yield (attribute, operator, value) for every set filter field.

        Fields whose value is `None` are treated as "not set" and skipped,
        so a filter instance with no values matches everything.
        """
        for field_name in type(self).model_fields:
            head, sep, op = field_name.rpartition(_SEPARATOR)
            if not sep or head == "" or op not in _OPS:
                continue
            value = getattr(self, field_name)
            if value is None:
                continue
            yield head, op, value


class AllSearchFilter(SearchFilter[T]):
    """Constant filter that matches every item (no restriction).

    The identity for conjunction: ``And([... All, f]) == f``. Its SQL
    condition is ``None`` (no WHERE clause).

    Use the ``ALL`` singleton instead of instantiating directly.
    """

    def matches(self, item: T) -> bool:
        return True

    def sql_condition(self) -> SqlCondition:
        return None


class NoneSearchFilter(SearchFilter[T]):
    """Constant filter that matches no item.

    Its SQL condition is ``false`` so any statement filtered by it yields
    zero rows. The identity for disjunction: ``Or([... None, f]) == f``.

    Use the ``NONE`` singleton instead of instantiating directly.
    """

    def matches(self, item: T) -> bool:
        return False

    def sql_condition(self) -> SqlCondition:
        return false()


# Singletons for stateless constant filters. These are typed as Any so they
# satisfy SearchFilter[T] for any T. Use these instead of instantiating
# AllSearchFilter/NoneSearchFilter directly.
ALL: AllSearchFilter[Any] = AllSearchFilter[Any]()
NONE: NoneSearchFilter[Any] = NoneSearchFilter[Any]()


class AttributeFilter(SearchFilter[T]):
    """Generic runtime-specified filter on a single attribute.

    Unlike ``BaseSearchFilter`` (which derives its predicates from statically
    declared field names), ``AttributeFilter`` accepts the attribute name,
    value, and condition at runtime. This is useful for permission scopes and
    dynamic queries where the filterable attributes are not known at schema
    definition time.

    The entity type ``T`` must be set via ``__class_getitem__`` parameterization
    (e.g. ``AttributeFilter[User]``) for SQL filtering to work; in-memory
    ``matches`` works against any object with the named attribute.
    """

    attribute: str
    value: Any
    condition: Condition

    def matches(self, item: T) -> bool:
        attr_value = getattr(item, self.attribute, None)
        _sql_builder, in_mem = _OPS[self.condition.value]
        return in_mem(attr_value, self.value)

    def sql_condition(self) -> SqlCondition:
        entity = type(self)._entity_cls
        if not isinstance(entity, type):
            raise TypeError(
                f"{type(self).__name__} is not parameterized with a concrete entity "
                "type; subclass as AttributeFilter[YourEntity] to enable SQL filtering."
            )
        column = getattr(entity, self.attribute, None)
        if column is None:
            raise AttributeError(f"{entity.__name__!r} has no attribute {self.attribute!r}")
        sql_builder, _ = _OPS[self.condition.value]
        return cast("SqlCondition", sql_builder(column, self.value))


class AndSearchFilter(SearchFilter[T]):
    """Conjunction of a list of search filters.

    An item matches iff it matches every child filter. The SQL condition is
    the ``AND`` of the children's conditions; a child with ``None`` condition
    (e.g. :class:`AllSearchFilter`) contributes nothing. An empty filter
    matches everything (empty conjunction is true).

    Prefer the ``and_filter()`` factory function which normalizes the result
    (removes redundant All/None filters, unwraps singletons, etc.).
    """

    filters: list[Any]

    @field_validator("filters", mode="before")
    @classmethod
    def _resolve_children(cls, value: Any) -> Any:
        """Resolve child dicts via the unparameterized base so the full
        discriminated-union registry (all subclasses) is available — a
        parameterized ``SearchFilter[T]`` has an empty subclass registry."""
        if not isinstance(value, list):
            return value
        return [SearchFilter.model_validate(v) if isinstance(v, dict) else v for v in value]

    def matches(self, item: T) -> bool:
        return all(f.matches(item) for f in self.filters)

    def sql_condition(self) -> SqlCondition:
        conditions: list[ColumnElement[bool]] = [
            c for c in (f.sql_condition() for f in self.filters) if c is not None
        ]
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return and_(*conditions)


class OrSearchFilter(SearchFilter[T]):
    """Disjunction of a list of search filters.

    An item matches iff it matches at least one child filter. The SQL
    condition is the ``OR`` of the children's conditions; if any child has a
    ``None`` condition (matches everything) the disjunction matches
    everything. An empty filter matches nothing (empty disjunction is false).

    Prefer the ``or_filter()`` factory function which normalizes the result
    (removes redundant All/None filters, unwraps singletons, etc.).
    """

    filters: list[Any]

    @field_validator("filters", mode="before")
    @classmethod
    def _resolve_children(cls, value: Any) -> Any:
        """Resolve child dicts via the unparameterized base (see AndSearchFilter)."""
        if not isinstance(value, list):
            return value
        return [SearchFilter.model_validate(v) if isinstance(v, dict) else v for v in value]

    def matches(self, item: T) -> bool:
        return any(f.matches(item) for f in self.filters)

    def sql_condition(self) -> SqlCondition:
        conditions: list[ColumnElement[bool]] = [
            c for c in (f.sql_condition() for f in self.filters) if c is not None
        ]
        # Any child that matches everything (None condition) makes the whole
        # disjunction match everything.
        if any(f.sql_condition() is None for f in self.filters):
            return None
        if not conditions:
            return false()
        if len(conditions) == 1:
            return conditions[0]
        return or_(*conditions)


# ---------------------------------------------------------------------------
# Factory functions for normalized composite filters
# ---------------------------------------------------------------------------


def and_filter(*filters: SearchFilter[T]) -> SearchFilter[T]:
    """Construct a normalized conjunction of filters.

    Applies algebraic identities to return the simplest equivalent filter:
    - And with All is simplified (All is identity for AND)
    - And with None yields None (None is annihilator for AND)
    - Empty And yields All (empty conjunction = true)
    - Singleton And is unwrapped
    - Nested And filters are flattened.
    """
    children: list[SearchFilter[T]] = []
    for f in filters:
        if f is NONE or isinstance(f, NoneSearchFilter):
            return NONE  # Annihilator — short-circuit
        if f is ALL or isinstance(f, AllSearchFilter):
            continue  # Identity — skip
        if isinstance(f, AndSearchFilter):
            # Flatten nested And (recursively normalized children)
            for child in f.filters:
                if child is NONE or isinstance(child, NoneSearchFilter):
                    return NONE
                if child is ALL or isinstance(child, AllSearchFilter):
                    continue
                children.append(child)
        else:
            children.append(f)

    if not children:
        return ALL  # Empty conjunction = true
    if len(children) == 1:
        return children[0]  # Unwrap singleton
    return AndSearchFilter(filters=children)


def or_filter(*filters: SearchFilter[T]) -> SearchFilter[T]:
    """Construct a normalized disjunction of filters.

    Applies algebraic identities to return the simplest equivalent filter:
    - Or with None is simplified (None is identity for OR)
    - Or with All yields All (All is annihilator for OR)
    - Empty Or yields None (empty disjunction = false)
    - Singleton Or is unwrapped
    - Nested Or filters are flattened.
    """
    children: list[SearchFilter[T]] = []
    for f in filters:
        if f is ALL or isinstance(f, AllSearchFilter):
            return ALL  # Annihilator — short-circuit
        if f is NONE or isinstance(f, NoneSearchFilter):
            continue  # Identity — skip
        if isinstance(f, OrSearchFilter):
            # Flatten nested Or (recursively normalized children)
            for child in f.filters:
                if child is ALL or isinstance(child, AllSearchFilter):
                    return ALL
                if child is NONE or isinstance(child, NoneSearchFilter):
                    continue
                children.append(child)
        else:
            children.append(f)

    if not children:
        return NONE  # Empty disjunction = false
    if len(children) == 1:
        return children[0]  # Unwrap singleton
    return OrSearchFilter(filters=children)
