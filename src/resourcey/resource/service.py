"""``SqlService`` - the SQL-backed service for a resource.

A service exposes the standard actions (create, read, update, delete, search,
count, batch_read, batch_edit) as async methods whose signatures carry **no**
notion of session, user, or RBAC (issue #40). The ``AsyncSession`` is instance
state on :class:`SqlService`, set at construction; the abstract
:class:`~resourcey.resource.service_base.BaseService` interface is
storage-agnostic. This is what makes a service trivially wrappable: an RBAC
wrapper holds an inner service plus a user and delegates, with no signature
changes.

``SqlService`` contains logic (validation orchestration, error mapping, PATCH
merge, pagination assembly, sort validation) and delegates data access to a
:class:`~resourcey.resource.repository.ResourceRepository`. It is usable
independently of HTTP - call its methods directly. Route mounting lives in
:mod:`resourcey.resource.routes` and asks the resource for a service via
:meth:`~resourcey.resource.sql.SqlResource.open_service`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.cache.cache_header import CacheHeader
from resourcey.resource.cursor import decode_cursor, encode_cursor
from resourcey.resource.errors import InvalidInputError, NotFoundError
from resourcey.resource.repository import ResourceRepository
from resourcey.resource.service_base import BaseService

if TYPE_CHECKING:
    from resourcey.encryption.encryption_service import EncryptionService
    from resourcey.resource.sql import SqlResource
    from resourcey.util.search_filter import SearchFilter


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of cursor-paginated search results.

    ``next_cursor`` is an opaque, encrypted keyset cursor pointing at the last
    row of this page; pass it as the ``cursor`` query param on the next
    request to fetch the following page. It is ``None`` when this page is the
    last (no more rows follow). There is no ``total`` - counting is a separate
    ``count`` action (issue #35).
    """

    items: list[T]
    limit: int
    next_cursor: str | None


# Default pagination bounds. ``limit`` is capped so a client cannot request
# an unbounded scan.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


class SqlService(BaseService):
    """The SQL-backed service exposing the standard resource actions.

    Constructed from a :class:`~resourcey.resource.sql.SqlResource` subclass
    and an ``AsyncSession`` (the session is instance state, not a per-call
    parameter, so the action signatures stay storage-agnostic). Resolves the
    create / update / read models and id field from the resource and caches
    the repository instance. Each action is an overridable async method.
    """

    def __init__(
        self,
        resource: type[SqlResource],
        *,
        session: AsyncSession,
        repository_cls: type[ResourceRepository] | None = None,
        serialization_context: dict[str, Any] | None = None,
    ) -> None:
        self.resource = resource
        self.create_model = resource.get_create_model()
        self.update_model = resource.get_update_model()
        self.read_model = resource.get_read_model()
        self.id_field = resource.get_id_field()
        self.repository = (repository_cls or ResourceRepository)(resource)
        self._serialization_context = serialization_context
        self._session = session

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The pydantic serialization context used for secret fields.

        Flows to ``model_dump`` / ``model_validate`` in the repository so
        ``SecretStr`` fields encrypt on write and decrypt on read. ``None``
        means no context (secrets redact on dump - only appropriate when the
        resource has no secret fields). Override or pass
        ``serialization_context`` at construction to supply an
        ``encryption_service`` / ``expose_secrets`` context.
        """
        return self._serialization_context

    def _ctx(self) -> dict[str, Any] | None:
        return self.serialization_context()

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: BaseModel) -> Any:
        """Validate via the create model, persist, return the read model (HTTP 201)."""
        return await self.repository.insert(self._session, payload, context=self._ctx())

    async def read(self, id: Any) -> Any:  # noqa: A002
        """Fetch; raise :class:`NotFoundError` (-> 404) if absent."""
        result = await self.repository.get_by_id(self._session, id, context=self._ctx())
        if result is None:
            raise NotFoundError(self.resource.__name__, id)
        return result

    async def update(self, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        """Validate via the PATCH update model, apply; raise ``NotFoundError`` if absent."""
        result = await self.repository.update_by_id(self._session, id, payload, context=self._ctx())
        if result is None:
            raise NotFoundError(self.resource.__name__, id)
        return result

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete; raise ``NotFoundError`` if absent. Returns no body (HTTP 204)."""
        deleted = await self.repository.delete_by_id(self._session, id)
        if not deleted:
            raise NotFoundError(self.resource.__name__, id)

    async def search(
        self,
        *,
        limit: int = _DEFAULT_LIMIT,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: SearchFilter[Any] | None = None,
    ) -> Page[Any]:
        """Search with cursor pagination, sort, and optional filters; return a :class:`Page`.

        ``cursor`` is an opaque, encrypted keyset cursor from a previous
        page's ``next_cursor``; ``None`` (or omitted) starts from the first
        page. ``sort`` is a single sortable field name (validated against the
        resource's ``sortable`` flag); ``desc`` selects descending order
        (default ascending). The returned ``next_cursor`` is ``None`` when
        this page is the last.
        """
        limit = self._validate_limit(limit)
        sort_parsed = self._parse_sort(sort, desc)
        decoded_cursor = self._decode_cursor(cursor, sort_parsed)
        # Fetch one extra row to detect whether a next page exists without a
        # separate count query (keyset pagination does not use total/offset).
        items = await self.repository.search(
            self._session,
            limit=limit + 1,
            sort=sort_parsed,
            filters=filters,
            cursor=decoded_cursor,
            context=self._ctx(),
        )
        has_next = len(items) > limit
        items = items[:limit]
        next_cursor = self._next_cursor(items, sort_parsed) if has_next else None
        return Page(items=items, limit=limit, next_cursor=next_cursor)

    async def count(
        self,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        """Return the number of rows matching ``filters`` (decoupled from paging/sort).

        Delegates to the repository's ``count()`` (``select(func.count())``
        with the same ``filters``).
        """
        return await self.repository.count(self._session, filters=filters)

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        """Return read models positionally aligned with the input ids.

        Each position ``i`` holds the read model for ``ids[i]`` or ``None`` if
        no such entity exists, so the response length always equals the input
        length and callers can correlate results by index.
        """
        return await self.repository.get_many_by_ids(self._session, ids, context=self._ctx())

    async def batch_edit(
        self,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        """Apply each edit (id + update payload); return results in input order.

        Maintains a 1:1 positional correspondence with the input edits: each
        position ``i`` holds the updated read model for ``edits[i]`` or
        ``None`` if that id does not exist (no DB write). The merge is
        idempotent - re-applying the same batch yields the same results and
        leaves absent ids untouched. Edits are a list of
        ``(id, update_model_instance)`` tuples.
        """
        results: list[Any] = []
        for edit_id, payload in edits:
            updated = await self.repository.update_by_id(
                self._session, edit_id, payload, context=self._ctx()
            )
            results.append(updated)
        return results

    # ------------------------------------------------------------------
    # Cache header computation (logic; delegates to the resource strategy)
    # ------------------------------------------------------------------

    def compute_cache_header(self, items: list[Any]) -> CacheHeader | None:
        """Resolve the resource's cache strategy and compute a header for ``items``.

        Returns the :class:`~resourcey.cache.cache_header.CacheHeader`, or
        ``None`` when the strategy yields no validators and no expiry (so the
        HTTP layer skips header setting entirely). The serialization context
        (``self._ctx()``) is threaded into the strategy so the ETag validates
        the same bytes the response body serializes to.
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
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_limit(self, limit: int) -> int:
        if limit < 1:
            raise InvalidInputError(f"limit must be >= 1, got {limit}")
        if limit > _MAX_LIMIT:
            limit = _MAX_LIMIT
        return limit

    def _encryption_service(self) -> EncryptionService:
        """The encryption service used to encrypt/decrypt cursors.

        Sourced from the serialization context (shared with at-rest field
        encryption) when present, otherwise the process-wide singleton. The
        singleton is always available in production (the encryption key is
        required config); tests set ``RESOURCEY_ENCRYPTION_KEY_*`` env vars.
        """
        from resourcey.encryption.encryption_service import get_encryption_service

        ctx = self._serialization_context
        if ctx is not None:
            enc = ctx.get("encryption_service")
            if enc is not None:
                return enc  # type: ignore[no-any-return]
        return get_encryption_service()

    def _sort_key_field(self, sort_parsed: tuple[str, bool] | None) -> str:
        """The field whose value the cursor keys off (id when no sort is requested)."""
        if sort_parsed is None:
            return self.id_field
        return sort_parsed[0]

    def _decode_cursor(
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
                self._encryption_service(), cursor
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

    def _next_cursor(
        self,
        items: list[Any],
        sort_parsed: tuple[str, bool] | None,
    ) -> str | None:
        """Encode a ``next_cursor`` from the last item, or ``None`` if the page is exhausted."""
        if not items:
            return None
        last = items[-1]
        field = self._sort_key_field(sort_parsed)
        sort_key = getattr(last, field)
        id_value = getattr(last, self.id_field)
        sort_field = sort_parsed[0] if sort_parsed is not None else None
        ascending = sort_parsed[1] if sort_parsed is not None else True
        return encode_cursor(
            self._encryption_service(),
            sort_field=sort_field,
            ascending=ascending,
            sort_key=sort_key,
            id_value=id_value,
        )

    def _parse_sort(self, sort: str | None, desc: bool) -> tuple[str, bool] | None:
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


# Re-export for backward-compatible imports (tests / app still import these
# names from ``resourcey.resource.service``).
__all__ = ["BaseService", "Page", "SqlService"]
