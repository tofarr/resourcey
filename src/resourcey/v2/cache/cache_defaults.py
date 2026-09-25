"""Default cache-strategy selection for a resource (the ``v2`` migration).

The single place that picks the strategy a resource uses when it does not
override :meth:`~resourcey.v2.core.resource.Resource.get_cache_strategy`. It is
storage-agnostic: a backend passes in the resource's derived read models and
advertised actions and gets a concrete strategy back, so the policy is not
duplicated per backend.

Selection, in order:

* a **read-only** resource — one advertising none of the write actions — gets
  :class:`~resourcey.v2.cache.cache_strategy.OptimisticCacheStrategy` with a
  freshness window (:data:`DEFAULT_READ_ONLY_EXPIRE_IN`) marked **private**. A
  read-only surface cannot change underneath a client, so a validator buys
  nothing and a freshness window lets clients skip revalidation. ``private`` is
  not optional: "read-only" does not imply "the same bytes for every caller" —
  a permission-narrowed search returns caller-scoped content, and without
  ``private`` a *shared* cache could replay one caller's body to another for
  the whole window (the API-key header is not ``Authorization``, so RFC 9111's
  authenticated-response protection does not apply);
* otherwise :class:`~resourcey.v2.cache.cache_strategy.LastModifiedCacheStrategy`
  when the read model carries an ``updated_at`` field (an accurate, cheap
  validator);
* otherwise :class:`~resourcey.v2.cache.cache_strategy.ETagCacheStrategy`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import cast

from resourcey.v2.cache.cache_strategy import (
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
    OptimisticCacheStrategy,
)
from resourcey.v2.core.dto import RestModels
from resourcey.v2.core.service import Action
from resourcey.v2.core.service import CacheStrategy as CoreCacheStrategy

# The freshness window a read-only resource gets by default (seconds). Read-only
# data does not change, so a client may reuse a copy for this long without
# revalidating.
DEFAULT_READ_ONLY_EXPIRE_IN = 600

# The write actions: a resource advertising none of these is read-only.
_WRITE_ACTIONS: frozenset[Action] = frozenset(
    {Action.CREATE, Action.UPDATE, Action.DELETE, Action.BATCH_EDIT}
)


def is_read_only(supported_actions: frozenset[Action]) -> bool:
    """Whether a resource is read-only (it advertises no write action)."""
    return supported_actions.isdisjoint(_WRITE_ACTIONS)


def default_cache_strategy(
    rest_models: RestModels, supported_actions: frozenset[Action]
) -> CoreCacheStrategy:
    """The default strategy for a resource with these read models and actions.

    ``rest_models`` is a :class:`~resourcey.v2.core.dto.RestModels`; its
    ``read_response`` fields decide between last-modified and ETag.
    ``supported_actions`` is the resource's advertised
    :class:`~resourcey.v2.core.service.Action` set — a read-only set selects the
    optimistic strategy before the read model is consulted.
    """
    if is_read_only(supported_actions):
        return OptimisticCacheStrategy(expire_in=DEFAULT_READ_ONLY_EXPIRE_IN, private=True)
    fields = getattr(rest_models.read_response, "model_fields", {})
    if "updated_at" in fields:
        return LastModifiedCacheStrategy()
    return ETagCacheStrategy()


class DefaultCacheStrategyMixin:
    """The default :meth:`get_cache_strategy` shared by v2 backends.

    A backend implements :meth:`get_rest_models` and
    :meth:`get_supported_actions` (both already part of the ``Resource``
    contract) and mixes this in to inherit the default policy — including the
    per-instance caching — instead of re-implementing it. The result is cached on
    the instance because one resource *class* can serve many models, so a
    class-level cache would hand one model's strategy to another.

    Both hooks stay ``@abstractmethod`` here (with no body) so mixing this in
    does not, by defining a concrete override, quietly drop them from the
    ``Resource`` ABC's ``__abstractmethods__``: a backend that forgets one
    still fails at instantiation rather than at the first request.

    Overriding :meth:`get_cache_strategy` on the concrete resource still wins:
    this is the default, not a sealed seam.
    """

    # Resolved lazily on first use; the ``__dict__`` lookup treats "absent" as
    # "not yet resolved", so no backend has to initialise it.
    _cache_strategy: CoreCacheStrategy | None = None

    @abstractmethod
    def get_rest_models(self) -> RestModels:
        """The six REST models (implemented by the concrete resource)."""

    @abstractmethod
    def get_supported_actions(self) -> frozenset[Action]:
        """The advertised actions (implemented by the concrete resource)."""

    def get_cache_strategy(self) -> CoreCacheStrategy:
        """The default strategy for this resource, cached on the instance."""
        cached = self.__dict__.get("_cache_strategy")
        if cached is None:
            cached = default_cache_strategy(self.get_rest_models(), self.get_supported_actions())
            self._cache_strategy = cached
        return cast("CoreCacheStrategy", cached)
