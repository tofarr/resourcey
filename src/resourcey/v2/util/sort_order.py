"""Storage-agnostic sort orders for ``v2`` (issue #97).

Mirrors :mod:`resourcey.v2.util.search_filter`: a :class:`SortOrder` is a
frozen, generic, discriminated-union Pydantic model with a single behavioural
method — :meth:`SortOrder.compare` — plus the shape information a backend needs
to translate it. Nothing here imports a storage library: the SQL translation
lives in ``v2/sql/sort_converter.py``, and this module is the bottom ``v2/util``
layer, importing only its sibling :mod:`resourcey.v2.util.models`.

:meth:`SortOrder.compare` is the in-memory *reference* semantics for an
ordering (used by an in-memory backend and pinned by ``specs/sorting.qnt``). It
orders by one attribute only; the identifier tie-breaker that makes paging a
total order is applied by the backend (an ``ORDER BY`` with the identifier
appended, and the matching keyset predicate), so it does not belong in the node.

The initial standard implementation is the ``v1`` surface: **a single
attribute, ascending or descending** (:class:`AttrSortOrder`). Multi-attribute /
composite sort is deliberately a later rung — the discriminated union is what
makes it additive.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import ConfigDict

from resourcey.v2.util.models import DiscriminatedUnionMixin

T = TypeVar("T")
ObjT = TypeVar("ObjT")
ValT = TypeVar("ValT")


class CompareResult(StrEnum):
    """How ``a`` orders relative to ``b`` under a :class:`SortOrder`."""

    LESS = "less"
    GREATER = "greater"
    SAME = "same"


class SortOrder(DiscriminatedUnionMixin, Generic[T]):
    """A frozen ordering over values of type ``T``, tagged by ``kind``.

    The only behaviour is :meth:`compare`; storage translation is a backend
    concern. Every field is part of the public structure a backend introspects,
    so a translator never needs to subclass or modify the node types.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def compare(self, a: T, b: T) -> CompareResult:
        """Order ``a`` relative to ``b`` (``LESS`` when ``a`` sorts first)."""
        raise NotImplementedError


class AttrSortOrder(SortOrder[ObjT], Generic[ObjT, ValT]):
    """Orders objects by one attribute, ascending or descending.

    ``ObjT`` is the object type; ``ValT`` is the attribute's type. Two type
    parameters are needed because the ordering is over ``ObjT`` while the
    compared values are ``ValT`` — a single parameter cannot express both.
    """

    attribute: str
    descending: bool = False

    def _get(self, value: ObjT) -> Any:
        if isinstance(value, Mapping):
            return value.get(self.attribute)
        return getattr(value, self.attribute, None)

    def compare(self, a: ObjT, b: ObjT) -> CompareResult:
        left, right = self._get(a), self._get(b)
        if left == right:
            return CompareResult.SAME
        result = CompareResult.LESS if _sorts_first(left, right) else CompareResult.GREATER
        return _reverse(result) if self.descending else result


def _sorts_first(left: Any, right: Any) -> bool:
    """Whether ``left`` precedes ``right`` in ascending order.

    ``None`` sorts first so the in-memory reference order is total even when the
    attribute is nullable; where a backend places NULL is its dialect's concern.
    """
    if left is None:
        return right is not None
    if right is None:
        return False
    return bool(left < right)


def _reverse(result: CompareResult) -> CompareResult:
    """The comparison with its operands swapped (``SAME`` is its own reverse)."""
    if result is CompareResult.LESS:
        return CompareResult.GREATER
    if result is CompareResult.GREATER:
        return CompareResult.LESS
    return result
