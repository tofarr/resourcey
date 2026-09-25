"""Cache validator value object (the ``v2`` migration of ``resourcey.cache.cache_header``).

A :class:`CacheHeader` carries the three cache validators a response may
advertise (``ETag`` / ``Last-Modified`` / freshness). It is HTTP-agnostic: it
knows nothing about request headers. Mapping ``If-None-Match`` /
``If-Modified-Since`` into a ``CacheHeader`` is the HTTP layer's job — the
``304`` decision then reduces to :meth:`CacheHeader.is_modified`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CacheHeader(BaseModel):
    """The cache validators + freshness a response can advertise.

    Each field is optional: a strategy that does not produce a validator
    leaves it ``None``. The HTTP layer only emits a header for a non-``None``
    field.

    Attributes:
        etag: A strong ETag (quoted opaque token), or ``None`` when the
            strategy does not produce one.
        updated_at: The origin server's last-modified time (UTC), or ``None``
            when the strategy does not produce one.
        expire_at: When the cached representation should be considered stale,
            or ``None`` when no freshness directive is emitted.
        private: Whether the freshness window is caller-scoped. When ``True``
            the HTTP layer emits ``Cache-Control: private`` so a shared cache
            (a proxy or CDN) must not store the response -- required when the
            body can differ per caller even though the resource is read-only.
    """

    etag: str | None = None
    updated_at: datetime | None = None
    expire_at: datetime | None = None
    private: bool = False

    def is_modified(self, other: CacheHeader) -> bool:
        """Whether the client's cached copy (``other``) is still current.

        Returns ``False`` (not modified) when the validators prove the
        client's copy is equivalent to the server's:

        * If ``self.etag`` is set: not modified when it equals any of the
          client's ETag tokens. A client may send a comma-separated list of
          ETags (RFC 7232 ``If-None-Match``) or ``*`` (match-if-exists); the
          latter always matches for an existing resource.
        * Else if ``self.updated_at`` is set: not modified when
          ``self.updated_at <= other.updated_at`` (the server's last change
          is at or before the client's ``If-Modified-Since`` time).
        * Else (no validators): always modified - the server cannot prove
          equivalence, so the body is sent.

        ``other`` is the client-supplied validators (mapped from
        ``If-None-Match`` / ``If-Modified-Since``): a missing client validator
        is represented by the corresponding ``None`` field, which can never
        satisfy the equality / ordering check, so the body is sent.
        """
        if self.etag is not None:
            if other.etag is None:
                return True
            client_tokens = {t.strip() for t in other.etag.split(",")}
            return self.etag not in client_tokens and "*" not in client_tokens
        if self.updated_at is not None:
            if other.updated_at is None:
                return True
            return self.updated_at > other.updated_at
        return True

    def has_any(self) -> bool:
        """Whether this header carries any validator or freshness directive."""
        return (
            self.etag is not None
            or self.updated_at is not None
            or self.expire_at is not None
            or self.private
        )
