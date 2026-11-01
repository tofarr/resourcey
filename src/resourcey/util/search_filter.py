"""Storage-agnostic search filters (issue #79).

A :class:`SearchFilter` is a frozen, generic, discriminated-union Pydantic model
with a single behavioural method — ``matches(value) -> bool`` — plus the shape
information a backend needs to translate it. Nothing here imports a storage
library; this is the bottom ``util`` layer.

Two levels of lowering:

1. **Standard filters** — the node types below. This is the tree a backend sees.
2. **Object filters** — :class:`BaseObjectFilter` subclasses declare
   ``<attribute>__<op>`` fields and lower *themselves* to a standard tree via
   :meth:`BaseObjectFilter.create_standard_filter`, so a custom object filter
   needs no backend handler.

Normalisation cannot live in ``__init__`` (a frozen model cannot return a
different type), so the algebraic identities live in :func:`and_`, :func:`or_`,
:func:`not_`, and :func:`attr`, which return the simplest equivalent node.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from types import UnionType
from typing import Annotated, Any, Generic, TypeVar, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PrivateAttr, SkipValidation, field_validator

from resourcey.util.models import DiscriminatedUnionMixin
from resourcey.util.singleton import Singleton

T = TypeVar("T")
ObjT = TypeVar("ObjT")
ValT = TypeVar("ValT")

# The separator in an ``<attribute>__<op>`` object-filter field name.
SEPARATOR = "__"

# The most values an :class:`InFilter` may carry. Beyond it, push the set down as
# a store join / subquery rather than a literal list.
MAX_IN_VALUES = 500

# A nested filter field is validated by the ``mode="before"`` resolvers below and
# then accepted as-is. Without ``SkipValidation`` a parameterised generic
# annotation re-enters the discriminated-union validator on an already-built
# instance, which the mixin cannot handle.
NestedFilter = Annotated["SearchFilter[Any]", SkipValidation]
NestedTuple = Annotated[tuple["SearchFilter[Any]", ...], SkipValidation]


def _resolve_child(value: Any) -> Any:
    """Resolve a wire ``dict`` into a concrete filter instance, else pass through."""
    if isinstance(value, dict):
        return SearchFilter.model_validate(value)
    return value


# ---------------------------------------------------------------------------
# SearchFilter — the abstract core
# ---------------------------------------------------------------------------


class SearchFilter(DiscriminatedUnionMixin, Generic[T]):
    """A frozen predicate over values of type ``T``, tagged by ``kind``.

    The only behaviour is :meth:`matches`; storage translation is a backend
    concern.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def matches(self, value: T) -> bool:
        """Whether ``value`` satisfies this filter.

        This is the reference semantics every backend's pushdown must agree
        with, so a node that cannot be evaluated in memory must raise here
        rather than return a plausible-but-wrong answer. A silent ``True`` or
        ``False`` makes the list backend (and the ``allow_filter_iteration``
        fallback) disagree with the database without any error.
        """
        raise NotImplementedError

    def create_standard_filter(self) -> SearchFilter[Any]:
        """This filter as a standard tree (identity for a standard node).

        :class:`BaseObjectFilter` overrides this to lower its fields.
        """
        return self

    def walk(self) -> Iterator[SearchFilter[Any]]:
        """This node and every descendant, depth-first, pre-order.

        A backend uses it to find the nodes it must resolve before converting
        (see ``SqlFilterConverter.resolve``). Descendants live under ``filters``
        (an iterable of filters) and / or ``filter`` (a single filter), which is
        where every standard node keeps them — a custom node nesting a subtree
        must use the same two names.
        """
        yield self
        for child in getattr(self, "filters", ()):
            yield from child.walk()
        nested = getattr(self, "filter", None)
        if isinstance(nested, SearchFilter):
            yield from nested.walk()


# ---------------------------------------------------------------------------
# Logical filters
# ---------------------------------------------------------------------------


class AllFilter(Singleton, SearchFilter[T]):
    """Constant filter that matches every value (the conjunction identity)."""

    def matches(self, value: T) -> bool:
        return True


class NoMatchFilter(Singleton, SearchFilter[T]):
    """Constant filter that matches no value (the disjunction identity)."""

    def matches(self, value: T) -> bool:
        return False


class AndFilter(SearchFilter[T]):
    """Conjunction: matches iff every child matches (empty conjunction is true)."""

    filters: NestedTuple

    @field_validator("filters", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        if isinstance(value, tuple | list):
            return tuple(_resolve_child(child) for child in value)
        return value

    def matches(self, value: T) -> bool:
        return all(child.matches(value) for child in self.filters)


class OrFilter(SearchFilter[T]):
    """Disjunction: matches iff any child matches (empty disjunction is false)."""

    filters: NestedTuple

    @field_validator("filters", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        if isinstance(value, tuple | list):
            return tuple(_resolve_child(child) for child in value)
        return value

    def matches(self, value: T) -> bool:
        return any(child.matches(value) for child in self.filters)


class NotFilter(SearchFilter[T]):
    """Logical inversion of a wrapped filter."""

    filter: NestedFilter

    @field_validator("filter", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_child(value)

    def matches(self, value: T) -> bool:
        return not self.filter.matches(value)


# ---------------------------------------------------------------------------
# Attribute filter
# ---------------------------------------------------------------------------


class AttrFilter(SearchFilter[ObjT], Generic[ObjT, ValT]):
    """Applies a value filter to one attribute of the object.

    Two type parameters are needed: the wrapped filter is ``SearchFilter[ValT]``
    while this node is a ``SearchFilter[ObjT]``.
    """

    attribute: str
    filter: NestedFilter

    @field_validator("filter", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_child(value)

    def _get(self, value: ObjT) -> Any:
        if isinstance(value, Mapping):
            return value.get(self.attribute)
        return getattr(value, self.attribute, None)

    def matches(self, value: ObjT) -> bool:
        return self.filter.matches(self._get(value))


# ---------------------------------------------------------------------------
# Value filters
# ---------------------------------------------------------------------------


class EqFilter(SearchFilter[T]):
    """Value equality (``value == stored``)."""

    value: T

    def matches(self, value: T) -> bool:
        return bool(value == self.value)


class GtFilter(SearchFilter[T]):
    """Strict greater-than (``value > stored``)."""

    value: T

    def matches(self, value: T) -> bool:
        return bool(value > self.value)  # type: ignore[operator]


class GeFilter(SearchFilter[T]):
    """Greater-than-or-equal (``value >= stored``)."""

    value: T

    def matches(self, value: T) -> bool:
        return bool(value >= self.value)  # type: ignore[operator]


class LtFilter(SearchFilter[T]):
    """Strict less-than (``value < stored``)."""

    value: T

    def matches(self, value: T) -> bool:
        return bool(value < self.value)  # type: ignore[operator]


class LeFilter(SearchFilter[T]):
    """Less-than-or-equal (``value <= stored``)."""

    value: T

    def matches(self, value: T) -> bool:
        return bool(value <= self.value)  # type: ignore[operator]


class ContainsFilter(SearchFilter[T]):
    """Substring / membership: ``stored`` contains this filter's ``value``.

    The *stored* attribute value is the haystack and the filter's ``value`` the
    needle. Strings compare case-insensitively; other values fall back to
    membership.
    """

    value: T

    def matches(self, value: T) -> bool:
        return _mem_contains(value, self.value)


class InFilter(SearchFilter[T]):
    """Set membership: the stored value is one of ``values``.

    ``values`` is a **tuple** so the frozen tree stays hashable. An **empty set
    matches nothing** (fail-closed): it is a deny, never "no restriction".

    ``values`` is capped at :data:`MAX_IN_VALUES`; beyond it a store join /
    subquery is the escape hatch.
    """

    values: tuple[T, ...]

    @property
    def value(self) -> tuple[T, ...]:
        """Alias of :attr:`values` so the generic converters (which read
        ``node.value``) handle this leaf without a special case."""
        return self.values

    @field_validator("values", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("values")
    @classmethod
    def _enforce_cap(cls, value: tuple[T, ...]) -> tuple[T, ...]:
        if len(value) > MAX_IN_VALUES:
            raise ValueError(
                f"InFilter permits at most {MAX_IN_VALUES} values; got {len(value)}. "
                "Push the set down as a store join / subquery (the materialized-ACL "
                "path) instead of a literal IN list."
            )
        return value

    def matches(self, value: T) -> bool:
        return value in self.values


def _mem_contains(attr_value: Any, needle: Any) -> bool:
    """In-memory ``needle in stored`` with case-insensitive string semantics."""
    if attr_value is None:
        return False
    if isinstance(attr_value, str) and isinstance(needle, str):
        return needle.lower() in attr_value.lower()
    try:
        return bool(needle in attr_value)
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Factory functions (the home of normalisation)
# ---------------------------------------------------------------------------


def and_(*filters: SearchFilter[Any]) -> SearchFilter[Any]:
    """Normalised conjunction.

    ``NoMatch`` is an annihilator (any child yields ``NoMatch``), ``All`` is an
    identity (dropped), nested ``And`` is flattened, a single surviving child is
    unwrapped, and an empty conjunction is ``All``.
    """
    children: list[SearchFilter[Any]] = []
    for child in _flatten(filters, AndFilter):
        if isinstance(child, NoMatchFilter):
            return NoMatchFilter()
        if isinstance(child, AllFilter):
            continue
        children.append(child)
    if not children:
        return AllFilter()
    if len(children) == 1:
        return children[0]
    return AndFilter(filters=tuple(children))


def or_(*filters: SearchFilter[Any]) -> SearchFilter[Any]:
    """Normalised disjunction.

    ``All`` is an annihilator (any child yields ``All``), ``NoMatch`` is an
    identity (dropped), nested ``Or`` is flattened, a single surviving child is
    unwrapped, and an empty disjunction is ``NoMatch``.
    """
    children: list[SearchFilter[Any]] = []
    for child in _flatten(filters, OrFilter):
        if isinstance(child, AllFilter):
            return AllFilter()
        if isinstance(child, NoMatchFilter):
            continue
        children.append(child)
    if not children:
        return NoMatchFilter()
    if len(children) == 1:
        return children[0]
    return OrFilter(filters=tuple(children))


def not_(filter_: SearchFilter[Any]) -> SearchFilter[Any]:
    """Normalised inversion: double negation unwraps, constants swap."""
    if isinstance(filter_, NotFilter):
        return filter_.filter
    if isinstance(filter_, AllFilter):
        return NoMatchFilter()
    if isinstance(filter_, NoMatchFilter):
        return AllFilter()
    return NotFilter(filter=filter_)


def attr(attribute: str, filter_: SearchFilter[Any]) -> SearchFilter[Any]:
    """Normalised attribute binding.

    Binding a constant to an attribute does not change the constant, so an
    ``All`` / ``NoMatch`` child is returned directly.
    """
    if isinstance(filter_, AllFilter | NoMatchFilter):
        return filter_
    return AttrFilter(attribute=attribute, filter=filter_)


def _flatten(filters: Iterable[SearchFilter[Any]], node_type: type[Any]) -> list[SearchFilter[Any]]:
    """Flatten one level of the same composite node type (recursively)."""
    out: list[SearchFilter[Any]] = []
    for child in filters:
        if isinstance(child, node_type):
            out.extend(_flatten(child.filters, node_type))
        else:
            out.append(child)
    return out


# ---------------------------------------------------------------------------
# BaseObjectFilter — the ergonomic ``<attribute>__<op>`` surface
# ---------------------------------------------------------------------------

# Object-filter operator suffix -> standard leaf node type.
_OP_NODES: dict[str, Any] = {
    "eq": EqFilter,
    "gt": GtFilter,
    "ge": GeFilter,
    "lt": LtFilter,
    "le": LeFilter,
    "contains": ContainsFilter,
    "in": InFilter,
}


class BaseObjectFilter(SearchFilter[T]):
    """A filter whose clauses are declared as ``<attribute>__<op>`` fields.

    The base reflects over its own Pydantic fields, collects every non-``None``
    value, and lowers them to a standard filter tree, cached in a private
    attribute (never a field, so it stays out of ``model_dump``). An unset filter
    lowers to ``AllFilter``.
    """

    _standard_filter: SearchFilter[Any] | None = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        # ``object.__setattr__`` because the model is frozen.
        object.__setattr__(self, "_standard_filter", self._build_standard_filter())

    def create_standard_filter(self) -> SearchFilter[Any]:
        """The cached standard tree this object filter lowers to."""
        if self._standard_filter is None:  # pragma: no cover - set in model_post_init
            object.__setattr__(self, "_standard_filter", self._build_standard_filter())
        return cast("SearchFilter[Any]", self._standard_filter)

    def matches(self, value: T) -> bool:
        return self.create_standard_filter().matches(value)

    def _build_standard_filter(self) -> SearchFilter[Any]:
        clauses: list[SearchFilter[Any]] = []
        for name in type(self).model_fields:
            head, sep, op = name.rpartition(SEPARATOR)
            if not sep or head == "" or op not in _OP_NODES:
                continue
            value = getattr(self, name)
            if value is None:
                continue
            clauses.append(attr(head, _leaf_for(op, value)))
        return and_(*clauses)


# ---------------------------------------------------------------------------
# Deriving a query surface from a read model
# ---------------------------------------------------------------------------

# Operator suffixes by base Python type.
_IN = frozenset({"in"})
_ORDERED_OPS = frozenset({"gt", "ge", "lt", "le"})
_EQ_ONLY = frozenset({"eq"}) | _IN
_STRING_OPS = _EQ_ONLY | _ORDERED_OPS | {"contains"}
_ORDERABLE_OPS = _EQ_ONLY | _ORDERED_OPS

_ORDERABLE_TYPES = (int, float, Decimal, datetime, date, time)
_STRING_TYPES = (str, UUID)
# A JSON column projects to a ``dict`` / ``list`` annotation; none of these is
# scalarly comparable, so a filter on one would push an unsupported operator.
_CONTAINER_TYPES = (dict, list, set, frozenset, tuple)


def _base_annotation(annotation: Any) -> Any:
    """Strip ``Optional`` / ``Annotated`` down to the underlying scalar type.

    A generic container is returned as-is, so :func:`operators_for_annotation`
    can reject it rather than mistake a ``list[str]`` column for a ``str``.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _base_annotation(get_args(annotation)[0])
    if origin in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _base_annotation(args[0])
    return annotation


def operators_for_annotation(annotation: Any) -> frozenset[str]:
    """The ``<op>`` suffixes a field of ``annotation`` supports.

    A non-scalar annotation (a JSON ``dict`` / list column, or a model-typed
    column) yields **no** operators: it is not scalarly comparable.
    """
    base = _base_annotation(annotation)
    if isinstance(base, type):
        if issubclass(base, bool):
            return _EQ_ONLY
        if issubclass(base, _CONTAINER_TYPES):
            return frozenset()
        if issubclass(base, BaseModel):
            # A structured payload (a model-typed column) is not scalarly
            # comparable: there is no query-param syntax for it, so it is not
            # filterable (matching the container case above).
            return frozenset()
        if issubclass(base, _ORDERABLE_TYPES):
            return _ORDERABLE_OPS
        if issubclass(base, _STRING_TYPES):
            return _STRING_OPS
        return _EQ_ONLY
    return frozenset()


def build_filter(clauses: Iterable[tuple[str, str, Any]]) -> SearchFilter[Any]:
    """Combine ``(attribute, op, value)`` clauses into one normalised tree."""
    children = [attr(attribute, _leaf_for(op, value)) for attribute, op, value in clauses]
    return and_(*children)


def _leaf_for(op: str, value: Any) -> SearchFilter[Any]:
    """Construct the value leaf for ``op``, naming its field correctly.

    The set leaf takes ``values``; a comma-separated string (the wire form of a
    set) is split into a tuple and a bare scalar is wrapped.
    """
    if op == "in":
        if isinstance(value, str):
            value = tuple(part.strip() for part in value.split(",") if part.strip())
        elif not isinstance(value, (tuple, list, set, frozenset)):
            value = (value,)
        return InFilter(values=tuple(value))
    leaf: SearchFilter[Any] = _OP_NODES[op](value=value)
    return leaf
