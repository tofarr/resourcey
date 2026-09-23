"""``PagedService`` - shared paging, sort, cursor, and cache logic.

The storage-agnostic half of the search contract: the parts of
:class:`~resourcey.resource.service_base.BaseService` that are pure logic
(``limit`` validation, ``sort`` parsing, opaque-cursor decode/encode, cache
header computation) and therefore identical across every storage backend.

``SqlService`` and ``MongoService`` each implemented their own copy of this
logic; :class:`PagedService` hoists it so a new backend (e.g.
:class:`~resourcey.list.list_service.ListService`) reuses it instead of
copy-pasting. A subclass supplies only what is genuinely storage-specific:
the :class:`Page` type via :class:`~resourcey.resource.service_base.BaseService`
and, for the no-sort cursor default, the resource's id field.

The sort field / direction encoded in a cursor are validated on decode, so a
cursor built for ``sort=size`` reused under ``sort=created_at`` (or no sort)
is rejected rather than silently applied against the wrong column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from resourcey.cache.cache_header import CacheHeader
from resourcey.resource.cursor import decode_cursor, encode_cursor
from resourcey.resource.errors import InvalidInputError
from resourcey.resource.service_base import BaseService

if TYPE_CHECKING:
    from resourcey.encryption.encryption_service import EncryptionService
    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter

# Default pagination bounds. ``limit`` is capped so a client cannot request
# an unbounded scan. Kept here (rather than in each backend) so every service
# advertises the same paging contract.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class PagedService(BaseService):
    """A :class:`BaseService` with shared search paging / sort / cursor logic.

    Concrete backends subclass this and implement the action methods against
    their storage, reusing:

    * :meth:`validate_limit` - clamp / reject ``limit``.
    * :meth:`parse_sort` - validate a sort field against the resource.
    * :meth:`decode_cursor` / :meth:`next_cursor` - opaque keyset cursors.
    * :meth:`compute_cache_header` / :meth:`compute_count_cache_header`.

    ``resource`` and ``id_field`` are resolved at construction (the resource
    owns the id field, the sortable fields, and the cache strategy, so all
    three are available to every subclass without further wiring).
    """

    def __init__(self, resource: BaseResource) -> None:
        self.resource = resource
        self.id_field = resource.get_id_field()

    def serialization_context(self) -> dict[str, Any] | None:
        """The pydantic serialization context used for secret fields.

        ``None`` means no context (secrets redact on dump). A subclass that
        manages secret fields overrides this to supply an
        ``encryption_service`` / ``expose_secrets`` context.
        """
        return None

    def _ctx(self) -> dict[str, Any] | None:
        """Alias for :meth:`serialization_context` (used by route handlers)."""
        return self.serialization_context()

    # ------------------------------------------------------------------
    # Cache header computation (delegates to the resource strategy)
    # ------------------------------------------------------------------

    def compute_cache_header(self, items: list[Any]) -> CacheHeader | None:
        """Resolve the resource's cache strategy and compute a header for ``items``.

        Returns ``None`` when the strategy yields no validators and no expiry
        (so the HTTP layer skips header setting entirely). The serialization
        context is threaded into the strategy so the ETag validates the same
        bytes the response body serializes to.
        """
        header = self.resource.get_cache_strategy().get_cache_header(items, context=self._ctx())
        return header if header.has_any() else None

    def compute_count_cache_header(
        self,
        count: int,
        filters: SearchFilter[Any] | None,
    ) -> CacheHeader | None:
        """Compute a count-derived cache header for the ``count`` route.

        ``count`` returns a bare integer, not read-model instances, so the
        model-based ``get_cache_header`` does not apply. The ETag is a stable
        hash of the count value together with the canonical-JSON serialization
        of the resolved filter (distinct filters get distinct ETags). No
        ``Last-Modified`` (a row delete changes the count without touching any
        ``updated_at``, so last-modified is an unreliable validator for a
        count). ``expire_in`` from the resource's strategy is honoured.
        """
        from resourcey.cache.cache_strategy import _digest, _stable_json

        strategy = self.resource.get_cache_strategy()
        parts: list[bytes] = [str(count).encode("utf-8"), b"\n"]
        if filters is not None:
            parts.append(_stable_json(filters.model_dump(mode="json")).encode("utf-8"))
        header = CacheHeader(etag=f'"{_digest(parts)}"')
        return strategy.with_expiry(header) if header.has_any() else None

    # ------------------------------------------------------------------
    # Validation / paging helpers
    # ------------------------------------------------------------------

    def validate_limit(self, limit: int) -> int:
        """Reject a non-positive ``limit`` and cap it at :data:`MAX_LIMIT`."""
        if limit < 1:
            raise InvalidInputError(f"limit must be >= 1, got {limit}")
        return min(limit, MAX_LIMIT)

    def parse_sort(self, sort: str | None, desc: bool) -> tuple[str, bool] | None:
        """Map a sort field name + ``desc`` flag to a ``(field, ascending)`` tuple.

        Validates the field against the resource's ``sortable`` flag (unknown
        / non-sortable fields -> :class:`InvalidInputError`). Returns ``None``
        when no sort is requested. ``ascending`` is ``not desc``.
        """
        if not sort:
            return None
        if sort not in self.resource.model_fields:
            raise InvalidInputError(f"Unknown sort field {sort!r}")
        field = self.resource.model_fields[sort]
        config = self.resource.get_config_for_field(sort, field)
        if not config.sortable:
            raise InvalidInputError(f"Field {sort!r} is not sortable")
        return sort, not desc

    def sort_key_field(self, sort_parsed: tuple[str, bool] | None) -> str:
        """The field whose value the cursor keys off (id when no sort is requested)."""
        if sort_parsed is None:
            return self.id_field
        return sort_parsed[0]

    def encryption_service(self) -> EncryptionService:
        """The encryption service used to encrypt/decrypt cursors.

        Sourced from the serialization context (shared with at-rest field
        encryption) when present, otherwise the process-wide singleton.
        """
        from resourcey.encryption.encryption_service import get_encryption_service

        ctx = self.serialization_context()
        if ctx is not None:
            enc = ctx.get("encryption_service")
            if enc is not None:
                return enc  # type: ignore[no-any-return]
        return get_encryption_service()

    def decode_cursor(
        self,
        cursor: str | None,
        sort_parsed: tuple[str, bool] | None,
    ) -> tuple[Any, Any] | None:
        """Decrypt an opaque cursor into a ``(sort_key, id)`` pair, or ``None``.

        Validates that the cursor was built for the same ``(sort_field,
        ascending)`` as the current request: a cursor from a ``sort=size``
        page reused under ``sort=created_at`` (or no sort) would apply the
        decrypted key against the wrong column, yielding silently wrong
        results, so it is rejected with ``400 invalid_input``.
        """
        if not cursor:
            return None
        try:
            c_field, c_ascending, sort_key, id_value = decode_cursor(
                self.encryption_service(), cursor
            )
        except (ValueError, KeyError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc
        expected_field = sort_parsed[0] if sort_parsed is not None else None
        expected_ascending = sort_parsed[1] if sort_parsed is not None else True
        if c_field != expected_field or c_ascending != expected_ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; "
                "start a new search without a cursor when changing sort."
            )
        return sort_key, id_value

    def next_cursor(
        self,
        items: list[Any],
        sort_parsed: tuple[str, bool] | None,
    ) -> str | None:
        """Encode a ``next_cursor`` from the last item, or ``None`` if exhausted."""
        if not items:
            return None
        last = items[-1]
        field = self.sort_key_field(sort_parsed)
        sort_key = getattr(last, field)
        id_value = getattr(last, self.id_field)
        sort_field = sort_parsed[0] if sort_parsed is not None else None
        ascending = sort_parsed[1] if sort_parsed is not None else True
        return encode_cursor(
            self.encryption_service(),
            sort_field=sort_field,
            ascending=ascending,
            sort_key=sort_key,
            id_value=id_value,
        )
