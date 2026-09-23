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
:meth:`~resourcey.resource.sql.SqlResource.build_service`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.resource.errors import NotFoundError
from resourcey.resource.paged_service import DEFAULT_LIMIT, PagedService
from resourcey.resource.repository import ResourceRepository
from resourcey.resource.service_base import BaseService

if TYPE_CHECKING:
    from resourcey.resource.base import BaseResource
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


class SqlService(PagedService):
    """The SQL-backed service exposing the standard resource actions.

    Constructed from a :class:`~resourcey.resource.base.BaseResource` (a
    ``SqlResource`` or a wrapper delegating to one) and an ``AsyncSession``
    (the session is instance state, not a per-call parameter, so the action
    signatures stay storage-agnostic). Resolves the create / update / read
    models and id field from the resource and caches the repository instance.
    Each action is an overridable async method.
    """

    def __init__(
        self,
        resource: BaseResource,
        *,
        session: AsyncSession,
        repository_cls: type[ResourceRepository] | None = None,
        serialization_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(resource)
        self.create_model = resource.get_create_model()
        self.update_model = resource.get_update_model()
        self.read_model = resource.get_read_model()
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
            raise NotFoundError(type(self.resource).__name__, id)
        return result

    async def update(self, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        """Validate via the PATCH update model, apply; raise ``NotFoundError`` if absent."""
        result = await self.repository.update_by_id(self._session, id, payload, context=self._ctx())
        if result is None:
            raise NotFoundError(type(self.resource).__name__, id)
        return result

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete; raise ``NotFoundError`` if absent. Returns no body (HTTP 204)."""
        deleted = await self.repository.delete_by_id(self._session, id)
        if not deleted:
            raise NotFoundError(type(self.resource).__name__, id)

    async def search(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
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
        limit = self.validate_limit(limit)
        sort_parsed = self.parse_sort(sort, desc)
        decoded_cursor = self.decode_cursor(cursor, sort_parsed)
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
        next_cursor = self.next_cursor(items, sort_parsed) if has_next else None
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


# Re-export for backward-compatible imports (tests / app still import these
# names from ``resourcey.resource.service``).
__all__ = ["BaseService", "Page", "SqlService"]
