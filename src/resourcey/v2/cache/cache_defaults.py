"""Default cache-strategy selection for a resource (the ``v2`` migration).

The single place that picks the strategy a resource uses when it does not
override :meth:`~resourcey.v2.core.resource.Resource.get_cache_strategy`:

* :class:`~resourcey.v2.cache.cache_strategy.LastModifiedCacheStrategy` when the
  read model carries a ``updated_at`` field (an accurate, cheap validator);
* :class:`~resourcey.v2.cache.cache_strategy.ETagCacheStrategy` otherwise.

Both default to ``expire_in=0`` (validators only; the HTTP layer then forces
revalidation with ``Cache-Control: no-cache``).

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from typing import Any

from resourcey.v2.cache.cache_strategy import (
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
)
from resourcey.v2.core.service import CacheStrategy as CoreCacheStrategy


def default_cache_strategy(rest_models: Any) -> CoreCacheStrategy:
    """The default strategy for a resource whose read model is ``rest_models.read_response``.

    ``rest_models`` is a :class:`~resourcey.v2.core.dto.RestModels`; the
    ``read_response`` model's fields decide between last-modified and ETag.
    """
    fields = getattr(rest_models.read_response, "model_fields", {})
    if "updated_at" in fields:
        return LastModifiedCacheStrategy()
    return ETagCacheStrategy()
