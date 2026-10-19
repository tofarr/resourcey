"""Default cache-strategy selection for a resource.

The single place that picks the strategy a resource uses when it does not
override :meth:`~resourcey.core.resource.Resource.get_cache_strategy`. It is
storage-agnostic: a backend passes in the resource's derived read models and
advertised actions and gets a concrete strategy back.

Selection, in order:

* a **read-only** resource gets
  :class:`~resourcey.cache.cache_strategy.OptimisticCacheStrategy` with a
  freshness window (:data:`DEFAULT_READ_ONLY_EXPIRE_IN`) marked **private**.
  ``private`` is not optional: "read-only" does not imply "the same bytes for
  every caller", and without it a *shared* cache could replay one caller's
  permission-narrowed body to another for the whole window;
* otherwise :class:`~resourcey.cache.cache_strategy.LastModifiedCacheStrategy`
  when the read model carries an ``updated_at`` field;
* otherwise :class:`~resourcey.cache.cache_strategy.ETagCacheStrategy`.

This module imports no code outside the framework.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import cast

from resourcey.cache.cache_strategy import (
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
    OptimisticCacheStrategy,
)
from resourcey.core.dto import RestModels
from resourcey.core.service import Action
from resourcey.core.service import CacheStrategy as CoreCacheStrategy

# The freshness window a read-only resource gets by default (seconds).
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

    A read-only action set selects the optimistic strategy before the read model
    is consulted; otherwise the ``read_response`` fields decide between
    last-modified and ETag.
    """
    if is_read_only(supported_actions):
        return OptimisticCacheStrategy(expire_in=DEFAULT_READ_ONLY_EXPIRE_IN, private=True)
    fields = getattr(rest_models.read_response, "model_fields", {})
    if "updated_at" in fields:
        return LastModifiedCacheStrategy()
    return ETagCacheStrategy()


class DefaultCacheStrategyMixin:
    """The default :meth:`get_cache_strategy` shared by the backends.

    The result is cached on the instance because one resource *class* can serve
    many models, so a class-level cache would hand one model's strategy to
    another.

    Both hooks stay ``@abstractmethod`` here so mixing this in does not, by
    defining a concrete override, quietly drop them from the ``Resource`` ABC's
    ``__abstractmethods__``.
    """

    # Resolved lazily on first use; the ``__dict__`` lookup treats "absent" as
    # "not yet resolved".
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
