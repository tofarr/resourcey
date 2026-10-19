"""Polymorphic cache strategies for resources.

A :class:`CacheStrategy` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`
(so it participates in the polymorphic config / serialization machinery exactly
like a search filter) generic over the read-model type ``T``. Each strategy
produces a :class:`~resourcey.cache.cache_header.CacheHeader` from a list of
read models. The shared ``expire_in`` field (seconds, ``>= 0``) drives the
``Cache-Control: max-age`` / ``Expires`` directives; ``0`` means no freshness
directive is emitted.

Three strategies ship by default:

* :class:`ETagCacheStrategy` — a stable strong ETag over the serialized read
  models; no ``updated_at``.
* :class:`LastModifiedCacheStrategy` — ``max(item.updated_at)``; no ``etag``.
* :class:`OptimisticCacheStrategy` — only a freshness window (``expire_in >
  0``); no validators.

The concrete base also extends :class:`resourcey.core.service.CacheStrategy`,
the placeholder ``core`` names so that layer stays dependency-free. A
resource's :meth:`~resourcey.core.resource.Resource.get_cache_strategy` hook
returns a concrete strategy (or ``None`` for no caching) and may be overridden —
the single seam for cache policy.

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

# Truncate the SHA-256 hex digest to this many characters for the ETag. Long
# enough to make collisions impractical for cache equivalence, short enough to
# keep response headers compact.
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

    ``exclude_unset=False`` ensures fields defaulted by the server (e.g.
    ``created_at``) are included so the hash reflects the full representation.
    ``context`` is threaded into ``model_dump`` so secret-bearing fields
    serialize exactly as they do in the response body — the ETag then
    validates the bytes actually sent (a non-deterministic encryption IV changes
    both the body and the ETag together). ``None`` context redacts secrets,
    matching a body serialized without a context.
    """
    dumped = item.model_dump(mode="json", exclude_unset=False, context=context)
    return json.dumps(dumped, sort_keys=True, separators=(",", ":"))


def _stable_json(value: Any) -> str:
    """Recursively key-sorted, compact JSON for hashing arbitrary values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CacheStrategy(DiscriminatedUnionMixin, CoreCacheStrategy, ABC, Generic[T]):
    """Abstract base for a cache strategy over read-model type ``T``.

    Concrete subclasses are :class:`~resourcey.util.models.DiscriminatedUnionMixin`
    (a ``kind`` computed field tags the concrete type) and also extend
    :class:`resourcey.core.service.CacheStrategy`, so a strategy satisfies the
    core-level seam while its behaviour lives here. The shared ``expire_in``
    field (seconds) is validated ``>= 0``; when ``> 0`` the strategy sets
    ``expire_at`` on the header, which the HTTP layer translates to
    ``Cache-Control: max-age=<expire_in>`` and an ``Expires`` header.

    ``private`` marks the freshness window as caller-scoped: the HTTP layer
    then emits ``Cache-Control: private`` so a shared cache (proxy / CDN) does
    not store the response. A read-only resource whose body can still differ
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

        ``context`` is the pydantic serialization context (the same one used to
        serialize the response body) so ETag hashing matches the bytes sent.
        """
        raise NotImplementedError

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Whether a fresh read from the source is required (default: always).

        The *programmatic* freshness question a server-side cache asks: given
        when its entry was written (``read_at``) and the validator it holds
        (``etag``), does it need to hit the source again? ``True`` = re-read (the
        copy is stale), ``False`` = the cached copy is still good. Async so a
        strategy that must consult storage can, without a signature change later.

        The base reads every time (a policy that says nothing cannot prove
        freshness), and each shipped strategy overrides it: optimistic is a pure
        time check, last-modified compares the source's ``updated_at``, and ETag
        degenerates to read-then-compare. A missing ``read_at`` (an empty cache)
        always reads.
        """
        return True

    def count_cache_header(self, count: int, filters: Any = None) -> CacheHeader:
        """A count-derived header for the ``count`` route.

        ``count`` returns a bare integer, not read-model instances, so the
        model-based :meth:`get_cache_header` does not apply. The ETag is a
        stable hash of the count value together with the canonical-JSON
        serialization of the resolved filter (distinct filters get distinct
        ETags). No ``Last-Modified``: a row delete changes the count without
        touching any ``updated_at``, so last-modified is an unreliable
        validator for a count. ``expire_in`` is honoured.
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

        ``True`` when the strategy's freshness window is already private.
        Deliberately reports only the strategy's *own* contract, not the
        resource's: a service-level privacy flag (an ``Owner``-scoped resource)
        is layered on top by the transport via :meth:`with_private`.
        """
        return self.private

    def with_private(self, private: bool) -> CacheStrategy[T]:
        """A copy of this strategy with ``private`` forced to ``private``.

        The transport calls this with ``True`` when the request's service is
        caller-scoped, so even a validator-only strategy (ETag / Last-Modified)
        emits ``Cache-Control: private`` and a shared cache neither stores nor
        revalidates the response for another caller. ``private=False`` returns
        ``self`` unchanged, so the common (non-scoped) path allocates nothing.
        """
        if not private or self.private:
            return self
        return self.model_copy(update={"private": True})


class ETagCacheStrategy(CacheStrategy[T]):
    """Strong-ETag strategy: a stable hash of the serialized read models.

    Each item is assumed to be a Pydantic model. The ETag is a SHA-256 hex
    digest (truncated, quoted) of the canonical JSON of every item's
    ``model_dump(mode="json", exclude_unset=False, context=context)`` sorted
    by key, concatenated over the list in order. No ``updated_at`` is
    produced.

    :meth:`should_read` **cannot avoid the source read**: the tag is a hash of
    the content, so deciding whether the cached copy is current means reading
    the content and comparing. It saves materialisation / diffing and the
    network transfer of an equal body, not the round trip.
    """

    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        parts: list[bytes] = []
        for item in models:
            # Batch results may carry positional ``None`` for absent ids; they
            # hold no representational state, so they are skipped rather than
            # hashed (two results with the same present items are cache-equivalent
            # regardless of where the gaps sit).
            if item is None:
                continue
            parts.append(_canonical_json(item, context).encode("utf-8"))
            # A separator guards against adjacency ambiguity (the concatenation
            # of [a, b] vs [ab] for a single-item list).
            parts.append(b"\n")
        return self.with_expiry(CacheHeader(etag=f'"{_digest(parts)}"'))

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Always ``True``: an ETag cannot be decided without reading the source.

        The validator is a hash of the content, so a server-side cache holding it
        still has to fetch the body to recompute and compare — this degenerates
        to read-then-compare, not a read-free freshness check.
        """
        return True


class LastModifiedCacheStrategy(CacheStrategy[T]):
    """Last-Modified strategy: ``max(item.updated_at)`` over the list.

    The value is taken from the resource's ``updated_at`` read-model field.
    Items missing ``updated_at`` contribute ``datetime.min`` (they do not
    advance the max). No ``etag`` is produced.

    :meth:`should_read` **cannot decide from the signature alone**: deciding
    whether the cached copy is current means comparing the source's *current*
    ``updated_at`` against the entry's ``read_at``, and that current value is not
    one of the parameters, so it must come from a cheap source read (metadata /
    a ``HEAD``-style call). The strategy therefore reports ``True`` (the wrapper
    must consult the source) — the same "read required" answer as ETag, but where
    ETag needs the full body, this one needs only the source's timestamp.
    """

    def get_cache_header(
        self, models: list[T], *, context: dict[str, Any] | None = None
    ) -> CacheHeader:
        # Aware min so naive/aware datetimes are normalized before comparison.
        # Truncated to whole seconds: HTTP ``Last-Modified`` is second-precision,
        # so the validator must carry that precision for ``is_modified`` to match
        # what a client echoes back via ``If-Modified-Since``.
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
        """Always ``True``: freshness needs the source's current ``updated_at``.

        That value is not carried by the signature, so a caller holding only
        ``read_at`` / ``etag`` cannot prove the copy current and must read the
        source's timestamp — cheaper than a full read, but still a source contact.
        """
        return True


class OptimisticCacheStrategy(CacheStrategy[T]):
    """Optimistic strategy: only a freshness window, no validators.

    ``expire_in`` is required to be ``> 0``. The HTTP layer emits
    ``Cache-Control: max-age=<expire_in>`` and ``Expires`` but no validators,
    so a client may serve a stale copy without revalidating within the window.

    :meth:`should_read` is the pure **time check** the strategy already encodes:
    the copy is good while ``read_at`` is within ``expire_in`` of now, and stale
    once it is not — no source contact at all.
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
        """Freshness only: the optimistic strategy emits no validators anywhere.

        The base ``count`` ETag would contradict this strategy's contract (no
        validator), so the count route gets the same freshness-only header as
        the read routes.
        """
        return self.with_expiry(CacheHeader())

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Whether the copy is older than the freshness window — a pure time check.

        ``False`` (the copy is good) while ``read_at`` is within ``expire_in``
        seconds of now, ``True`` once it is not. An absent ``read_at`` (an empty
        cache) always reads. No source contact — this is why optimistic is the
        strategy a caching wrapper can serve from memory without hitting the
        source at all.
        """
        if read_at is None:
            return True
        if read_at.tzinfo is None:
            read_at = read_at.replace(tzinfo=UTC)
        return utc_now() - read_at >= timedelta(seconds=self.expire_in)
