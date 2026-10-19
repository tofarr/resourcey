"""``CachedService`` — the read-through cache a ``CachedResource`` yields.

The service wrapper :class:`~resourcey.cache.cached_resource.CachedResource`
builds. It forwards every action to ``inner`` but, for reads, consults the
resource's :class:`~resourcey.cache.cache_strategy.CacheStrategy`
(:meth:`~resourcey.core.service.CacheStrategy.should_read`) against the copy it
holds in a :class:`~resourcey.cache.cache_store.CacheStore`: a fresh copy is
served from the store (no source read), a stale one is refetched and the store
refilled.

Read-through scope
------------------
Only the shape-preserving reads are cached: ``read`` / ``search`` / ``count`` /
``batch_read``. Every write (``create`` / ``update`` / ``delete`` /
``batch_edit``) is forwarded and then **evicts** the resource's entries, so a
write through the wrapper never serves a stale copy.

Caller-scoped responses
-----------------------
A response that may differ per caller is **never** stored:
``response_is_private()`` is true when either the wrapper was configured
``private`` or the inner service reports itself caller-scoped (an ``Owner``
policy). Such a service still works, it just always reads through — sharing a
principal-narrowed body across callers would be an authorization leak.

This module is part of ``resourcey.cache``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Generic, TypeVar, cast

from resourcey.cache.cache_store import CacheEntry, CacheStore
from resourcey.cache.cache_strategy import CacheStrategy
from resourcey.core.dto import utc_now
from resourcey.core.service import (
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


def _stable_key(value: Any) -> str:
    """A stable, compact string for a cache-key component (key-sorted JSON)."""
    dumped = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(dumped, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: str) -> str:
    """A truncated SHA-256 hex digest of ``value``, for a bounded cache key."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class CachedService(Service[T, K], Generic[T, K]):
    """A service proxy that serves fresh reads from cache and evicts on a write.

    It is its own async context manager, delegating the storage lifetime to the
    inner service (the "whoever opens the storage owns its commit and close"
    rule is unchanged). The :class:`CacheStore` is owned by the wrapping
    resource, not this per-request service, so the cache outlives a request.
    """

    def __init__(
        self,
        inner: Service[T, K],
        store: CacheStore,
        strategy: CacheStrategy[T] | None,
        *,
        private: bool,
        path: str,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._store = store
        self._strategy = strategy
        self._private = private
        self._path = path
        self._owns_inner = False

    # ------------------------------------------------------------------
    # Lifecycle (delegated to the inner service)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> CachedService[T, K]:
        await super().__aenter__()
        # An inner already entered (a composed DependencyBuilder opened it)
        # stays owned by whoever opened it -- adopting it here would
        # double-enter / double-close it.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context / cache privacy (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context (delegated verbatim)."""
        return self._inner.serialization_context()

    def response_is_private(self) -> bool:
        """Whether this response may differ per caller, so must not be cached.

        The wrapper's own ``private`` flag **or** the inner service's
        caller-scoping — an ``Owner``-scoped inner still makes the body
        principal-dependent, so the wrapper must not share it.
        """
        return self._private or self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Cache plumbing
    # ------------------------------------------------------------------

    def _key(self, operation: str, discriminator: str) -> str:
        """A cache key scoped to this resource, operation, and arguments."""
        return f"{self._path}:{operation}:{discriminator}"

    def _cacheable(self) -> bool:
        """Whether this service may read / write cache at all.

        No: when there is no strategy, or the response is caller-scoped. A
        caller-scoped response is never shared, so it always reads through.
        """
        return self._strategy is not None and not self.response_is_private()

    def _entry(self, payload: Any) -> CacheEntry:
        """A cache entry for ``payload``, with a best-effort validator.

        The strategy's ETag is computed where the payload shape allows it (a
        model, a list, a :class:`Page`, or a bare count); the shipped
        time-based strategy ignores it, so a shape the strategy cannot hash
        simply stores ``etag=None``.
        """
        etag: str | None = None
        strategy = self._strategy
        if strategy is not None:
            try:
                header = self._header_for(strategy, payload)
                etag = getattr(header, "etag", None)
            except Exception:
                etag = None
        return CacheEntry(payload=payload, read_at=utc_now(), etag=etag)

    @staticmethod
    def _header_for(strategy: CacheStrategy[Any], payload: Any) -> Any:
        """The strategy header for a payload of any read shape."""
        if isinstance(payload, Page):
            return strategy.get_cache_header(payload.items)
        if isinstance(payload, list):
            return strategy.get_cache_header(payload)
        if isinstance(payload, int):
            return strategy.count_cache_header(payload)
        return strategy.get_cache_header([payload])

    async def _read_through(self, key: str, fetch: Any) -> Any:
        """Serve ``key`` from cache when fresh, else ``fetch`` and refill it."""
        cacheable = self._cacheable()
        if cacheable:
            entry = await self._store.get(key)
            if entry is not None and not await self._strategy.should_read(  # type: ignore[union-attr]
                entry.read_at, entry.etag
            ):
                return entry.payload
        payload = await fetch()
        if cacheable:
            await self._store.set(key, self._entry(payload))
        return payload

    async def _evict(self) -> None:
        """Evict every cached entry for this resource (after a write)."""
        await self._store.clear(f"{self._path}:")

    # ------------------------------------------------------------------
    # Reads (read-through)
    # ------------------------------------------------------------------

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        return cast(
            "T",
            await self._read_through(self._key("read", str(id)), lambda: self._inner.read(id)),
        )

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        self._require_entered()
        key = self._key("batch_read", _digest(_stable_key([str(i) for i in ids])))
        return cast(
            "list[T | None]",
            await self._read_through(key, lambda: self._inner.batch_read(ids)),
        )

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Page[T]:
        self._require_entered()
        discriminator = _digest(
            _stable_key(
                {
                    "filter": search_filter.model_dump(mode="json") if search_filter else None,
                    "sort": sort_order.model_dump(mode="json") if sort_order else None,
                    "cursor": cursor,
                    "limit": limit,
                }
            )
        )
        key = self._key("search", discriminator)
        return cast(
            "Page[T]",
            await self._read_through(
                key, lambda: self._inner.search(search_filter, sort_order, cursor, limit)
            ),
        )

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        self._require_entered()
        discriminator = _digest(
            _stable_key(search_filter.model_dump(mode="json") if search_filter else None)
        )
        key = self._key("count", discriminator)
        return cast("int", await self._read_through(key, lambda: self._inner.count(search_filter)))

    # ------------------------------------------------------------------
    # Writes (forwarded, then evict)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        result = await self._inner.create(payload)
        await self._evict()
        return result

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        self._require_entered()
        result = await self._inner.update(payload, condition=condition)
        await self._evict()
        return result

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        self._require_entered()
        deleted = await self._inner.delete(id, condition=condition)
        await self._evict()
        return deleted

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        self._require_entered()
        results = await self._inner.batch_edit(edits)
        await self._evict()
        return results
