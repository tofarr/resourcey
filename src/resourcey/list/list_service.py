"""``ListService`` - the read-only service over an in-process list of objects.

A :class:`~resourcey.list.list_resource.ListResource` yields a ``ListService``
from :meth:`~resourcey.list.list_resource.ListResource.build_service`. The
service holds the resolved items as instance state (mirroring how
:class:`~resourcey.resource.service.SqlService` holds an ``AsyncSession``) and
implements the read subset of the standard
:class:`~resourcey.resource.service_base.BaseService` contract - ``read``,
``search``, ``count``, ``batch_read`` - entirely in memory.

Paging, sort validation, opaque keyset cursors, and cache-header computation
are inherited from :class:`~resourcey.resource.paged_service.PagedService`;
only the in-memory data access is implemented here. Filtering uses the
declared ``SearchFilter``'s ``matches`` predicate (the same predicate the SQL
and Mongo paths push down), so a filter behaves identically across backends.

Write actions (``create`` / ``update`` / ``delete`` / ``batch_edit``) are
intentionally left as the raising :class:`BaseService` defaults: a list
resource narrows its ``actions`` to the read set, so those methods are never
routed and never called.

Every output passes through the resource's
:meth:`~resourcey.resource.base.BaseResource.clone_for_output`, so a defensive
``ListResource`` hands out deep copies and a caller cannot mutate the served
collection through a result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from resourcey.resource.errors import NotFoundError
from resourcey.resource.paged_service import DEFAULT_LIMIT, PagedService
from resourcey.resource.service import Page

if TYPE_CHECKING:
    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


class ListService(PagedService):
    """The read-only service exposing ``read`` / ``search`` / ``count`` / ``batch_read``.

    Constructed from a :class:`~resourcey.resource.base.BaseResource` (a
    ``ListResource`` or a wrapper delegating to one) and the resolved items
    (instance state, not a per-call parameter). Items may be Pydantic models,
    mappings, or plain objects; each is projected into the resource's read
    model on demand, so the service never mutates the caller's collection.
    """

    def __init__(
        self,
        resource: BaseResource,
        *,
        items: list[Any],
        serialization_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(resource)
        self.read_model = resource.get_read_model()
        self._items = items
        self._serialization_context = serialization_context

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The pydantic serialization context (secret fields decrypt on read)."""
        return self._serialization_context

    # ------------------------------------------------------------------
    # Standard actions (read subset)
    # ------------------------------------------------------------------

    async def read(self, id: Any) -> Any:  # noqa: A002
        """Fetch the item whose id matches; raise :class:`NotFoundError` (-> 404) if absent."""
        for item in self._items:
            model = self._to_read_model(item)
            if getattr(model, self.id_field) == id:
                return model
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
        """Filter, sort, and cursor-paginate the items in memory; return a :class:`Page`.

        Semantics match the SQL / Mongo backends: ``filters`` is applied via the
        declared filter's ``matches`` predicate, ``sort`` is validated against
        the resource's sortable fields, and ``cursor`` is an opaque keyset
        cursor from a previous page's ``next_cursor``.
        """
        limit = self.validate_limit(limit)
        sort_parsed = self.parse_sort(sort, desc)
        decoded_cursor = self.decode_cursor(cursor, sort_parsed)

        models = [self._to_read_model(item) for item in self._items]
        if filters is not None:
            models = [m for m in models if filters.matches(m)]
        models = self._sort(models, sort_parsed)
        if decoded_cursor is not None:
            models = self._after_cursor(models, decoded_cursor, sort_parsed)

        # Fetch one extra to detect a next page without a separate count.
        has_next = len(models) > limit
        items = models[:limit]
        next_cursor = self.next_cursor(items, sort_parsed) if has_next else None
        return Page(items=items, limit=limit, next_cursor=next_cursor)

    async def count(
        self,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        """Return the number of items matching ``filters`` (decoupled from paging/sort)."""
        models = [self._to_read_model(item) for item in self._items]
        if filters is not None:
            models = [m for m in models if filters.matches(m)]
        return len(models)

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        """Return read models positionally aligned with the input ids.

        Each position ``i`` holds the read model for ``ids[i]`` or ``None`` if
        no such item exists, so the response length always equals the input
        length. Duplicate ids map to the same item.
        """
        if not ids:
            return []
        by_id = {
            getattr(model, self.id_field): model
            for model in (self._to_read_model(item) for item in self._items)
        }
        return [by_id.get(i) for i in ids]

    # ------------------------------------------------------------------
    # In-memory helpers
    # ------------------------------------------------------------------

    def _to_read_model(self, item: Any) -> Any:
        """Project an item into the resource's read-model instance.

        The item is first passed through the resource's ``clone_for_output``
        (a defensive ``ListResource`` deep-copies here, so a served object can
        never be mutated through). When the served object is already the read
        model — the common case, where the wrapped Pydantic model *is* the read
        model — it is returned as-is. Otherwise it is dumped to a dict first (so
        a resource read model is never confused with a differently-named caller
        model), mappings are used as-is, and any other object is projected
        field-by-field. Validation uses the serialization context so secret
        fields decrypt consistently.
        """
        served = self.resource.clone_for_output(item)
        if isinstance(served, self.read_model):
            return served
        if isinstance(served, BaseModel):
            data = served.model_dump()
        elif isinstance(served, Mapping):
            data = dict(served)
        else:
            data = {name: getattr(served, name, None) for name in self.read_model.model_fields}
        return self.read_model.model_validate(data, context=self._ctx())

    def _sort(self, models: list[Any], sort_parsed: tuple[str, bool] | None) -> list[Any]:
        """Stable sort by ``(sort_field, id)`` in the requested direction.

        Sorting by the id as a tie-breaker matches the keyset cursor's ordering
        (``(sort_key, id)``), so paging is deterministic when sort keys collide.
        Falls back to id-only ordering when no ``sort`` is requested.
        """
        field = self.sort_key_field(sort_parsed)
        ascending = sort_parsed[1] if sort_parsed is not None else True
        return sorted(
            models,
            key=lambda m: (getattr(m, field), getattr(m, self.id_field)),
            reverse=not ascending,
        )

    def _after_cursor(
        self,
        models: list[Any],
        decoded_cursor: tuple[Any, Any],
        sort_parsed: tuple[str, bool] | None,
    ) -> list[Any]:
        """Keep only models strictly after the ``(sort_key, id)`` cursor position.

        Mirrors :func:`resourcey.resource.cursor.keyset_predicate`: ascending
        keeps ``(sort_key, id) > (cursor_key, cursor_id)``; descending mirrors
        it. When the sort field is the id, the comparison collapses to the id
        alone.
        """
        cursor_key, cursor_id = decoded_cursor
        ascending = sort_parsed[1] if sort_parsed is not None else True
        field = self.sort_key_field(sort_parsed)
        if field == self.id_field:
            if ascending:
                return [m for m in models if getattr(m, field) > cursor_id]
            return [m for m in models if getattr(m, field) < cursor_id]
        if ascending:
            return [
                m
                for m in models
                if (getattr(m, field), getattr(m, self.id_field)) > (cursor_key, cursor_id)
            ]
        return [
            m
            for m in models
            if (getattr(m, field), getattr(m, self.id_field)) < (cursor_key, cursor_id)
        ]
