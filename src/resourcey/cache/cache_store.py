"""``CacheStore`` — the storage seam a caching resource wrapper holds copies in.

:class:`CacheStore` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`,
so a deployment selects a concrete store by ``kind`` (the class name) with no
code change. Only :class:`InMemoryCacheStore` ships; a Redis / memcached store
is a later rung with the same contract.

A store is its own async context manager, entered through the caching resource's
lifecycle, so a store that owns a client ties that client's lifetime to the app.

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

    ``payload`` is ``Any`` because the store is operation-agnostic (a DTO, a
    ``Page``, a list, or a bare count); the caller knows the shape it stored.
    """

    payload: Any
    read_at: datetime
    etag: str | None = None


class CacheStore(DiscriminatedUnionMixin, ABC):
    """The abstract storage a caching resource wrapper holds copies in.

    Keys are opaque strings the wrapper composes; a store only maps a key to a
    :class:`CacheEntry`.
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

        A write through the wrapper passes the resource's key prefix; ``None``
        clears the whole store.
        """


class InMemoryCacheStore(CacheStore):
    """The single-process default: a plain ``dict`` guarded by an ``asyncio.Lock``.

    Sufficient for a single-worker deployment. A multi-worker deployment needs a
    shared store (Redis), which is why the seam is pluggable.
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
