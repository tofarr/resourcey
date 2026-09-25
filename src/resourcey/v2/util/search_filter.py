"""Storage-agnostic search filters for ``v2`` (issue #79).

A :class:`SearchFilter` is a frozen, generic, discriminated-union Pydantic model
with a single behavioural method — ``matches(value) -> bool`` — plus the shape
information a backend needs to translate it. Nothing here imports a storage
library: the SQL translation lives in ``v2/sql/filter_converter.py`` and this
module is the bottom ``v2/util`` layer, importing only its sibling
:mod:`resourcey.v2.util.models` (plus :mod:`resourcey.v2.util.singleton`).

Two levels of lowering
----------------------
1. **Standard filters** — the node types below (``AllFilter``, ``NoMatchFilter``,
   ``AndFilter``, ``OrFilter``, ``NotFilter``, ``AttrFilter`` and the value
   leaves ``EqFilter`` / ``GtFilter`` / ``GeFilter`` / ``LtFilter`` / ``LeFilter``
   / ``ContainsFilter``). This is the tree a backend sees.
2. **Object filters** — :class:`BaseObjectFilter` subclasses declare
   ``<attribute>__<op>`` fields (the ergonomic REST shape) and lower *themselves*
   to a standard tree via :meth:`BaseObjectFilter.create_standard_filter`.

A backend therefore only ever converts a standard tree; a custom object filter
needs no backend handler at all, because it lowers first.

Normalisation lives in the factory functions
--------------------------------------------
A frozen Pydantic model cannot return a *different* type from ``__init__``, so
the algebraic identities live in :func:`and_`, :func:`or_`, :func:`not_`, and
:func:`attr` — which return the simplest equivalent node — rather than in a
validator. The node classes themselves remain the raw, un-normalised form.

Naming
------
Every node class ends in ``Filter``. That is consistent with ``v1``, avoids
colliding with SQLAlchemy's ``and_`` / ``or_`` / ``not_`` / ``Column.contains``
and the ``all`` / ``filter`` builtins, and — because
:attr:`~resourcey.v2.util.models.DiscriminatedUnionMixin.kind` is the class name
— makes the OpenAPI discriminator unambiguous for free.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar, cast, get_args, get_origin
from uuid import UUID

from pydantic import ConfigDict, PrivateAttr, SkipValidation, field_validator

from resourcey.v2.util.models import DiscriminatedUnionMixin
from resourcey.v2.util.singleton import Singleton

T = TypeVar("T")
ObjT = TypeVar("ObjT")
ValT = TypeVar("ValT")

# The separator in an ``<attribute>__<op>`` object-filter field name.
SEPARATOR = "__"

# A nested filter field annotated with this is validated by the ``mode="before"``
# resolvers below (which route a ``dict`` through ``SearchFilter.model_validate``)
# and then accepted as-is. Without ``SkipValidation`` a *parameterised* generic
# annotation (``SearchFilter[T]``) re-enters the discriminated-union validator on
# an already-built instance, which the mixin's ``data.pop("kind")`` path cannot
# handle. The resolver keeps the wire (dict) and in-memory (instance) paths both
# working, and the annotation keeps mypy honest about the nested value type.
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
    concern. Every field is part of the public structure a backend introspects,
    so a translator never needs to subclass or modify the node types.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def matches(self, value: T) -> bool:
        """Whether ``value`` satisfies this filter."""
        raise NotImplementedError

    def create_standard_filter(self) -> SearchFilter[Any]:
        """This filter as a standard tree (identity for a standard node).

        :class:`BaseObjectFilter` overrides this to lower its ``<attr>__<op>``
        fields; a backend only ever needs to convert the result.
        """
        return self


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

    ``ObjT`` is the object type; ``ValT`` is the attribute's type. Two type
    parameters are needed because the wrapped filter is ``SearchFilter[ValT]``
    while the filter itself is a ``SearchFilter[ObjT]`` — a single parameter
    cannot express both.
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

    Pinned semantics (the issue left this ambiguous): the *stored* attribute
    value is the haystack and the filter's ``value`` is the needle, so an
    ``AttrFilter("name", ContainsFilter(value="ali"))`` means "``name`` contains
    ``ali``" — which is what ``?name__contains=ali`` must mean. Strings compare
    case-insensitively as substrings; other values fall back to membership
    (``needle in stored``).
    """

    value: T

    def matches(self, value: T) -> bool:
        return _mem_contains(value, self.value)


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

# Object-filter operator suffix -> standard leaf node type. Suffixes mirror the
# node names (``ge`` / ``le``), not v1's ``gte`` / ``lte``.
_OP_NODES: dict[str, Any] = {
    "eq": EqFilter,
    "gt": GtFilter,
    "ge": GeFilter,
    "lt": LtFilter,
    "le": LeFilter,
    "contains": ContainsFilter,
}


class BaseObjectFilter(SearchFilter[T]):
    """A filter whose clauses are declared as ``<attribute>__<op>`` fields.

    Subclass it and declare optional fields named ``<attribute>__<op>`` (for
    example ``email__contains``, ``created_at__ge``). The base reflects over its
    own Pydantic fields, collects every non-``None`` value, and lowers them to a
    standard filter tree with :meth:`create_standard_filter` — cached once, on
    first match, in a private attribute (never a field, so it stays out of
    ``model_dump`` / iteration, which the count ETag relies on).

    An unset (or all-``None``) filter lowers to ``AllFilter`` and so matches
    everything. ``T`` is the entity type; a subclass may declare it via
    ``Generic`` parameterisation for documentation, but lowering does not depend
    on it.
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
            clauses.append(attr(head, _OP_NODES[op](value=value)))
        return and_(*clauses)


# ---------------------------------------------------------------------------
# Deriving a query surface from a read model
# ---------------------------------------------------------------------------

# Operator suffixes by base Python type. Equality is always allowed; ordering
# only where it is meaningful; ``contains`` only for strings.
_ORDERED_OPS = frozenset({"gt", "ge", "lt", "le"})
_EQ_ONLY = frozenset({"eq"})
_STRING_OPS = _EQ_ONLY | _ORDERED_OPS | {"contains"}
_ORDERABLE_OPS = _EQ_ONLY | _ORDERED_OPS

_ORDERABLE_TYPES = (int, float, Decimal, datetime, date, time)
_STRING_TYPES = (str, UUID)


def _base_annotation(annotation: Any) -> Any:
    """Strip ``Optional`` / ``Annotated`` down to the underlying scalar type."""
    if get_origin(annotation) is None:
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return _base_annotation(args[0])
    return annotation


def operators_for_annotation(annotation: Any) -> frozenset[str]:
    """The ``<op>`` suffixes a field of ``annotation`` supports.

    The derived query surface: a field is filterable exactly when the read model
    exposes it, and the operator set follows the field's type (equality always;
    ordering for numbers and datetimes; substring for strings).
    """
    base = _base_annotation(annotation)
    if isinstance(base, type):
        if issubclass(base, bool):
            return _EQ_ONLY
        if issubclass(base, _ORDERABLE_TYPES):
            return _ORDERABLE_OPS
        if issubclass(base, _STRING_TYPES):
            return _STRING_OPS
    return _EQ_ONLY


def build_filter(clauses: Iterable[tuple[str, str, Any]]) -> SearchFilter[Any]:
    """Combine ``(attribute, op, value)`` clauses into one normalised tree."""
    children = [attr(attribute, _OP_NODES[op](value=value)) for attribute, op, value in clauses]
    return and_(*children)
