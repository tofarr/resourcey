"""Polymorphic cache strategies for resources.

A :class:`CacheStrategy` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`
generic over the read-model type ``T``. Each strategy produces a
:class:`~resourcey.cache.cache_header.CacheHeader` from a list of read models.
The shared ``expire_in`` field (seconds, ``>= 0``) drives the
``Cache-Control: max-age`` / ``Expires`` directives; ``0`` emits no freshness
directive.

The concrete base also extends :class:`resourcey.core.service.CacheStrategy`, the
placeholder ``core`` names so that layer stays dependency-free.

This module imports no code outside the framework.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar

from pydantic import model_validator

from resourcey.cache.cache_header import CacheHeader
from resourcey.core.dto import utc_now
from resourcey.core.service import CacheStrategy as CoreCacheStrategy
from resourcey.util.models import DiscriminatedUnionMixin

T = TypeVar("T")

# Truncated to keep response headers compact; long enough that collisions are
# impractical.
_ETAG_DIGEST_LEN = 32


def _expire_at(expire_in: int) -> datetime | None:
    """The expiry timestamp when ``expire_in > 0``, else ``None``."""
    if expire_in <= 0:
        return None
    return utc_now() + timedelta(seconds=expire_in)


def _digest(parts: list[bytes]) -> str:
    """Truncated SHA-256 hex digest over the concatenated byte ``parts``."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part)
    return hasher.hexdigest()[:_ETAG_DIGEST_LEN]


def _canonical_json(item: Any, context: dict[str, Any] | None = None) -> str:
    """Stable JSON for hashing: recursively key-sorted, compact separators.

    ``exclude_unset=False`` includes server-defaulted fields so the hash reflects
    the full representation. ``context`` is threaded into ``model_dump`` so
    secret-bearing fields serialize exactly as they do in the response body, and
    the ETag validates the bytes actually sent.
    """
    dumped = item.model_dump(mode="json", exclude_unset=False, context=context)
    return json.dumps(dumped, sort_keys=True, separators=(",", ":"))


def _stable_json(value: Any) -> str:
    """Recursively key-sorted, compact JSON for hashing arbitrary values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CacheStrategy(DiscriminatedUnionMixin, CoreCacheStrategy, ABC, Generic[T]):
    """Abstract base for a cache strategy over read-model type ``T``.

    ``expire_in`` (seconds, ``>= 0``) becomes ``expire_at`` on the header, which
    the HTTP layer translates to ``Cache-Control: max-age`` / ``Expires``.

    ``private`` marks the freshness window as caller-scoped, so a shared cache
    does not store the response. A read-only resource whose body can still differ
    per caller (a permission-narrowed search) sets it.
    """

    expire_in: int = 0
    private: bool = False

    @model_validator(mode="after")
    def _validate_expire_in(self) -> CacheStrategy[T]:
        if self.expire_in < 0:
            raise ValueError(f"expire_in must be >= 0, got {self.expire_in}")
        return self

    @abstractmethod
    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        """Compute the cache header for the given read-model instances.

        ``context`` is the serialization context used for the response body, so
        ETag hashing matches the bytes sent.
        """
        raise NotImplementedError

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Whether a fresh read from the source is required (default: always).

        The *programmatic* freshness question a server-side cache asks. ``True``
        = re-read, ``False`` = the cached copy is still good. Async so a strategy
        that must consult storage can, without a signature change later.
        """
        return True

    def count_cache_header(self, count: int, filters: Any = None) -> CacheHeader:
        """A count-derived header for the ``count`` route.

        The ETag hashes the count together with the resolved filter, so distinct
        filters get distinct ETags. No ``Last-Modified``: a row delete changes
        the count without touching any ``updated_at``.
        """
        parts: list[bytes] = [str(count).encode("utf-8"), b"\n"]
        if filters is not None:
            dumped = filters.model_dump(mode="json") if hasattr(filters, "model_dump") else filters
            parts.append(_stable_json(dumped).encode("utf-8"))
        return self.with_expiry(CacheHeader(etag=f'"{_digest(parts)}"'))

    def with_expiry(self, header: CacheHeader) -> CacheHeader:
        """Attach ``expire_at`` from ``expire_in`` and ``private`` (in place)."""
        header.expire_at = _expire_at(self.expire_in)
        header.private = self.private
        return header

    def cache_is_private(self) -> bool:
        """Whether this strategy's responses are caller-scoped, so must not be shared.

        Reports only the strategy's *own* contract; a service-level privacy flag
        is layered on top by the transport via :meth:`with_private`.
        """
        return self.private

    def with_private(self, private: bool) -> CacheStrategy[T]:
        """A copy of this strategy with ``private`` forced to ``private``.

        The transport calls this with ``True`` when the request's service is
        caller-scoped, so even a validator-only strategy emits
        ``Cache-Control: private``. ``private=False`` returns ``self``.
        """
        if not private or self.private:
            return self
        return self.model_copy(update={"private": True})


class ETagCacheStrategy(CacheStrategy[T]):
    """Strong-ETag strategy: a stable hash of the serialized read models.

    No ``updated_at`` is produced. :meth:`should_read` cannot avoid the source
    read: the tag is a hash of the content, so deciding whether the cached copy
    is current means reading the content and comparing.
    """

    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        parts: list[bytes] = []
        for item in models:
            # Positional ``None`` for an absent id holds no representational
            # state, so it is skipped rather than hashed.
            if item is None:
                continue
            parts.append(_canonical_json(item, context).encode("utf-8"))
            # Guards against adjacency ambiguity between consecutive items.
            parts.append(b"\n")
        return self.with_expiry(CacheHeader(etag=f'"{_digest(parts)}"'))

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Always ``True``: an ETag cannot be decided without reading the source."""
        return True


class LastModifiedCacheStrategy(CacheStrategy[T]):
    """Last-Modified strategy: ``max(item.updated_at)`` over the list.

    Items missing ``updated_at`` do not advance the max. No ``etag`` is produced.
    :meth:`should_read` must consult the source's current ``updated_at``, which
    is cheaper than a full body read but still a source contact.
    """

    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        # Truncated to whole seconds: HTTP ``Last-Modified`` is second-precision,
        # so ``is_modified`` matches what a client echoes back via
        # ``If-Modified-Since``.
        latest = datetime.min.replace(tzinfo=UTC, microsecond=0)
        for item in models:
            updated = getattr(item, "updated_at", None)
            if updated is None:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            updated = updated.replace(microsecond=0)
            if updated > latest:
                latest = updated
        return self.with_expiry(CacheHeader(updated_at=latest))

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Always ``True``: freshness needs the source's current ``updated_at``."""
        return True


class OptimisticCacheStrategy(CacheStrategy[T]):
    """Optimistic strategy: only a freshness window, no validators.

    ``expire_in`` is required to be ``> 0``. A client may serve a stale copy
    without revalidating within the window, and :meth:`should_read` is a pure
    time check with no source contact at all.
    """

    @model_validator(mode="after")
    def _validate_expire_in_positive(self) -> OptimisticCacheStrategy[T]:
        if self.expire_in <= 0:
            raise ValueError(
                f"OptimisticCacheStrategy requires expire_in > 0, got {self.expire_in}"
            )
        return self

    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        return self.with_expiry(CacheHeader())

    def count_cache_header(self, count: int, filters: Any = None) -> CacheHeader:
        """Freshness only: the base ``count`` ETag would contradict this strategy."""
        return self.with_expiry(CacheHeader())

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Whether the copy is older than the freshness window — a pure time check.

        An absent ``read_at`` (an empty cache) always reads.
        """
        if read_at is None:
            return True
        if read_at.tzinfo is None:
            read_at = read_at.replace(tzinfo=UTC)
        return utc_now() - read_at >= timedelta(seconds=self.expire_in)
