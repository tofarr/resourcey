"""``CacheStore`` — the storage seam a caching resource wrapper holds copies in.

A :class:`~resourcey.cache.cached_resource.CachedResource` keeps a
:class:`CacheStore` and consults the resource's
:class:`~resourcey.cache.cache_strategy.CacheStrategy` (:meth:`CacheStrategy.should_read
<resourcey.core.service.CacheStrategy.should_read>`) to decide whether the copy
it holds is still good, serving from the store when it is and refilling it from
the source when it is not.

:class:`CacheStore` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`,
so a deployment selects a concrete store by ``kind`` (the class name) with no
code change — exactly like every other pluggable seam (``Channel``,
``FileStore``, ``DependencyBuilder``). Only :class:`InMemoryCacheStore` ships;
a Redis / memcached store is a later rung with the same contract.

A store is its own async context manager, entered through the caching resource's
lifecycle, so a store that owns a client ties that client's lifetime to the app
exactly as a ``SqlSessionManager`` / ``MongoClientManager`` does.

This module is part of ``resourcey.cache``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from resourcey.util.models import DiscriminatedUnionMixin


class CacheEntry(BaseModel):
    """One cached copy: the stored payload plus its freshness metadata.

    Attributes:
        payload: The value the source returned for the key — a DTO, a
            :class:`~resourcey.core.service.Page`, a list (``batch_read``), or a
            bare ``int`` (``count``). ``Any`` because the store is
            operation-agnostic; the caller knows the shape it stored.
        read_at: When this entry was written — the ``read_at`` a strategy's
            :meth:`~resourcey.core.service.CacheStrategy.should_read` compares
            against its freshness window.
        etag: The validator the strategy produced for this payload, or ``None``
            when the strategy yields no ETag. Held so a strategy that consults
            its validator can use it; the shipped time-based strategy ignores it.
    """

    payload: Any
    read_at: datetime
    etag: str | None = None


class CacheStore(DiscriminatedUnionMixin, ABC):
    """The abstract storage a caching resource wrapper holds copies in.

    Keys are opaque strings the wrapper composes
    (``<resource-path>:<operation>:<discriminator>``); a store only maps a key
    to a :class:`CacheEntry`. It is its own async context manager so a store
    backed by an external client can open and close it with the app.
    """

    async def __aenter__(self) -> CacheStore:
        """Open the store's client / connection (override to do real work)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the store's client / connection (override to do real work)."""
        return None

    @abstractmethod
    async def get(self, key: str) -> CacheEntry | None:
        """The entry stored under ``key``, or ``None`` when absent."""

    @abstractmethod
    async def set(self, key: str, entry: CacheEntry) -> None:
        """Store ``entry`` under ``key`` (overwriting any existing entry)."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the entry under ``key`` (a no-op when absent)."""

    @abstractmethod
    async def clear(self, prefix: str | None = None) -> None:
        """Remove every entry, or every entry whose key starts with ``prefix``.

        A write through the wrapper calls this with the resource's key prefix to
        evict the copies it just invalidated; ``prefix`` of ``None`` clears the
        whole store (the escape hatch for a manual flush).
        """


class InMemoryCacheStore(CacheStore):
    """The single-process default: a plain ``dict`` guarded by an ``asyncio.Lock``.

    Sufficient for a single-worker deployment and the dev / test path. A
    multi-worker or multi-process deployment needs a shared store (Redis), which
    is why the seam is pluggable rather than hard-wiring a dict.
    """

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._entries: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> CacheEntry | None:
        async with self._lock:
            return self._entries.get(key)

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._lock:
            self._entries[key] = entry

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self, prefix: str | None = None) -> None:
        async with self._lock:
            if prefix is None:
                self._entries.clear()
                return
            for key in [k for k in self._entries if k.startswith(prefix)]:
                del self._entries[key]
