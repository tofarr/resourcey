"""Polymorphic cache strategies for resources.

A :class:`CacheStrategy` is a ``DiscriminatedUnionMixin`` (so it participates
in the polymorphic config / serialization machinery exactly like
``SearchFilter``) generic over the read-model type ``T``. Each strategy
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

A resource's :meth:`~resourcey.resource.base.BaseResource.get_cache_strategy`
hook picks a default and may be overridden (the single seam for cache policy).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar

from pydantic import model_validator

from resourcey.cache.cache_header import CacheHeader
from resourcey.util.models import DiscriminatedUnionMixin

T = TypeVar("T")

# Truncate the SHA-256 hex digest to this many characters for the ETag. Long
# enough to make collisions impractical for cache equivalence, short enough to
# keep response headers compact.
_ETAG_DIGEST_LEN = 32


def _utcnow() -> datetime:
    """UTC now (aware). Centralized so tests can monkeypatch if needed."""
    return datetime.now(UTC)


def _expire_at(expire_in: int) -> datetime | None:
    """The expiry timestamp when ``expire_in > 0``, else ``None``."""
    if expire_in <= 0:
        return None
    return _utcnow() + timedelta(seconds=expire_in)


def _canonical_json(item: Any) -> str:
    """Stable JSON for hashing: keys sorted, no unset-omission reshaping.

    ``exclude_unset=False`` ensures fields defaulted by the server (e.g.
    ``created_at``) are included so the hash reflects the full visible
    representation. Secret-bearing fields already redact / encrypt to a fixed
    sentinel via the serialization context, so the hash is stable across
    requests with differing encryption contexts — the visible representation
    is what matters for cache equivalence.
    """
    dumped = item.model_dump(mode="json", exclude_unset=False)
    return repr(sorted(dumped.items(), key=lambda kv: kv[0]))


class CacheStrategy(DiscriminatedUnionMixin, ABC, Generic[T]):
    """Abstract base for a cache strategy over read-model type ``T``.

    Concrete subclasses participate in the discriminated-union machinery (a
    ``kind`` computed field tags the concrete type). The shared ``expire_in``
    field (seconds) is validated ``>= 0``; when ``> 0`` the strategy sets
    ``expire_at`` on the header, which the HTTP layer translates to
    ``Cache-Control: max-age=<expire_in>`` and an ``Expires`` header.
    """

    expire_in: int = 0

    @model_validator(mode="after")
    def _validate_expire_in(self) -> CacheStrategy[T]:
        if self.expire_in < 0:
            raise ValueError(f"expire_in must be >= 0, got {self.expire_in}")
        return self

    @abstractmethod
    def get_cache_header(self, models: list[T]) -> CacheHeader:
        """Compute the cache header for the given read-model instances."""
        raise NotImplementedError

    def _with_expiry(self, header: CacheHeader) -> CacheHeader:
        """Attach ``expire_at`` from ``expire_in`` (in place) and return it."""
        header.expire_at = _expire_at(self.expire_in)
        return header


class ETagCacheStrategy(CacheStrategy[T]):
    """Strong-ETag strategy: a stable hash of the serialized read models.

    Each item is assumed to be a Pydantic model. The ETag is a SHA-256 hex
    digest (truncated, quoted) of the canonical JSON of every item's
    ``model_dump(mode="json", exclude_unset=False)`` sorted by key, concatenated
    over the list in order. No ``updated_at`` is produced.
    """

    def get_cache_header(self, models: list[T]) -> CacheHeader:
        hasher = hashlib.sha256()
        for item in models:
            hasher.update(_canonical_json(item).encode("utf-8"))
            # A separator guards against adjacency ambiguity (the concatenation
            # of [a, b] vs [ab] for a single-item list).
            hasher.update(b"\n")
        digest = hasher.hexdigest()[:_ETAG_DIGEST_LEN]
        return self._with_expiry(CacheHeader(etag=f'"{digest}"'))


class LastModifiedCacheStrategy(CacheStrategy[T]):
    """Last-Modified strategy: ``max(item.updated_at)`` over the list.

    The value is taken from the resource's ``updated_at`` read-model field.
    Items missing ``updated_at`` contribute ``datetime.min`` (they do not
    advance the max). No ``etag`` is produced.
    """

    def get_cache_header(self, models: list[T]) -> CacheHeader:
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
        return self._with_expiry(CacheHeader(updated_at=latest))


class OptimisticCacheStrategy(CacheStrategy[T]):
    """Optimistic strategy: only a freshness window, no validators.

    ``expire_in`` is required to be ``> 0``. The HTTP layer emits
    ``Cache-Control: max-age=<expire_in>`` and ``Expires`` but no validators,
    so a client may serve a stale copy without revalidating within the window.
    """

    @model_validator(mode="after")
    def _validate_expire_in_positive(self) -> OptimisticCacheStrategy[T]:
        if self.expire_in <= 0:
            raise ValueError(
                f"OptimisticCacheStrategy requires expire_in > 0, got {self.expire_in}"
            )
        return self

    def get_cache_header(self, models: list[T]) -> CacheHeader:
        return self._with_expiry(CacheHeader())
