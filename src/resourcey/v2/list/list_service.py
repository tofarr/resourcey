"""``ListService`` — the read-only action layer of the ``v2`` list backend (issue #116).

A :class:`~resourcey.v2.list.list_resource.ListResource` yields a
``ListService`` from
:meth:`~resourcey.v2.list.list_resource.ListResource.get_service`. The service
holds the resolved items as instance state and implements the read subset of the
standard :class:`~resourcey.v2.core.service.Service` contract — ``read`` /
``search`` / ``count`` / ``batch_read`` — entirely in memory.

Filtering uses the storage-agnostic :meth:`SearchFilter.matches` predicate and
ordering the :meth:`SortOrder.compare` reference — the same semantics the SQL /
Mongo converters push down — so a filter or sort behaves identically across
backends. Paging reuses the shared tamper-proof keyset cursor codec
(:mod:`resourcey.v2.util.cursor`) and mirrors the SQL ``keyset_predicate``
in memory, so a cursor round-trips and a cursor reused under a different sort is
rejected rather than applied against the wrong field.

Items are converted to the resource's DTO type before being returned, so the
transport projects DTO -> the derived REST model exactly as it does for SQL /
Mongo. Write actions (``create`` / ``update`` / ``delete`` / ``batch_edit``)
keep the raising :class:`Service` defaults: a list resource advertises only the
read actions, so those methods are never routed.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.core.service import (
    DEFAULT_LIMIT,
    STORAGE_KEY,
    NotFoundError,
    Page,
    Service,
)
from resourcey.v2.util.cursor import decode_cursor, encode_cursor
from resourcey.v2.util.search_filter import SearchFilter
from resourcey.v2.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.v2.encryption.encryption_service import EncryptionService
    from resourcey.v2.list.list_resource import ListResource

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


class ListService(Service[T, K]):
    """The read-only service over an in-process list of Pydantic objects.

    Holds the call-scoped ``ctx`` and the resolved items (instance state, not a
    per-call parameter). There is no external storage to open, so entering the
    service only adopts the items into ``ctx`` (matching the other backends'
    reuse seam) and the lifecycle guard.
    """

    def __init__(
        self,
        resource: ListResource[T, K],
        ctx: MutableMapping[Any, Any],
        items: list[Any],
    ) -> None:
        super().__init__()
        self._resource = resource
        self._ctx = ctx
        self._items = items

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> ListService[T, K]:
        await super().__aenter__()
        self._ctx.setdefault(STORAGE_KEY, self._items)
        return self

    # ------------------------------------------------------------------
    # Standard actions (read subset)
    # ------------------------------------------------------------------

    async def read(self, id: K) -> T:  # noqa: A002
        """Fetch the item whose identifier matches; raise :class:`NotFoundError` if absent."""
        self._require_entered()
        id_field = self._resource.get_id_field()
        for item in self._items:
            model = self._to_dto(item)
            if getattr(model, id_field) == id:
                return model
        raise NotFoundError(id)

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        """Filter, order, and keyset-page the items in memory; return a :class:`Page`.

        ``search_filter`` is a standard :class:`SearchFilter` tree (an object
        filter is lowered first) applied via ``matches``; ``sort_order`` is the
        validated ordering (``None`` for the default identifier order) applied
        via ``compare``, with the identifier appended as a stable tie-breaker.
        ``cursor`` is an opaque keyset cursor from a previous page; a cursor
        built for a different ``(sort field, direction)`` is rejected.
        """
        self._require_entered()
        models = [self._to_dto(item) for item in self._items]
        if search_filter is not None:
            standard = search_filter.create_standard_filter()
            models = [model for model in models if standard.matches(model)]
        models = self._order(models, sort_order)
        if cursor is not None:
            models = self._after_cursor(models, cursor, sort_order)
        has_more = len(models) > limit
        page_items = models[:limit]
        next_cursor = (
            self._next_cursor(page_items[-1], sort_order) if has_more and page_items else None
        )
        return Page(items=page_items, limit=limit, next_cursor=next_cursor)

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        """Return the number of items matching ``search_filter`` (all when ``None``)."""
        self._require_entered()
        models = [self._to_dto(item) for item in self._items]
        if search_filter is not None:
            standard = search_filter.create_standard_filter()
            models = [model for model in models if standard.matches(model)]
        return len(models)

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        """Return DTOs positionally aligned with ``ids`` (``None`` for absent)."""
        self._require_entered()
        if not ids:
            return []
        id_field = self._resource.get_id_field()
        by_id = {getattr(model, id_field): model for model in self._read_all()}
        return [by_id.get(i) for i in ids]

    # ------------------------------------------------------------------
    # In-memory ordering + paging
    # ------------------------------------------------------------------

    def _read_all(self) -> list[T]:
        """Every served item projected into a DTO."""
        return [self._to_dto(item) for item in self._items]

    def _order(self, models: list[T], sort_order: SortOrder[T] | None) -> list[T]:
        """Order ``models`` by the resolved sort, with the identifier as tie-breaker.

        The identifier is always appended ascending (the tie-breaker the SQL
        ``ORDER BY`` / ``keyset_predicate`` use), so paging is total. ``None``
        sorts first ascending and last descending (the reference ordering
        :meth:`AttrSortOrder.compare` describes), which the two-pass stable sort
        below reproduces.
        """
        id_field = self._resource.get_id_field()
        ordered = sorted(models, key=lambda model: getattr(model, id_field))
        attribute = getattr(sort_order, "attribute", None)
        if attribute is None:
            return ordered
        descending = bool(getattr(sort_order, "descending", False))
        return sorted(
            ordered,
            key=lambda model: _ascending_key(getattr(model, attribute)),
            reverse=descending,
        )

    def _after_cursor(
        self, models: list[T], cursor: str, sort_order: SortOrder[T] | None
    ) -> list[T]:
        """Keep only the models strictly after the cursor row (the keyset walk).

        Mirrors :func:`resourcey.v2.sql.cursor.keyset_predicate`: the sort-key
        comparison mirrors for descending, the identifier tie-breaker never does,
        and the null block is seeked past with explicit ``None`` tests (NULLs
        first ascending, last descending).
        """
        _cursor_field, cursor_ascending, cursor_key, cursor_id = self._decode(cursor, sort_order)
        id_field = self._resource.get_id_field()
        attribute = getattr(sort_order, "attribute", None)
        if attribute is None or attribute == id_field:
            if cursor_ascending:
                return [m for m in models if getattr(m, id_field) > cursor_id]
            return [m for m in models if getattr(m, id_field) < cursor_id]
        return [
            m
            for m in models
            if _after_cursor_item(
                getattr(m, attribute),
                getattr(m, id_field),
                cursor_key=cursor_key,
                cursor_id=cursor_id,
                ascending=cursor_ascending,
            )
        ]

    def _next_cursor(self, model: T, sort_order: SortOrder[T] | None) -> str:
        """Encode the cursor pointing past ``model`` under the resolved ``sort_order``."""
        id_field = self._resource.get_id_field()
        id_value = getattr(model, id_field)
        attribute = getattr(sort_order, "attribute", None)
        sort_key = id_value if attribute is None else getattr(model, attribute)
        return encode_cursor(
            self._encryption(),
            sort_field=attribute,
            ascending=not bool(getattr(sort_order, "descending", False)),
            sort_key=sort_key,
            id_value=id_value,
        )

    def _decode(
        self, cursor: str, sort_order: SortOrder[T] | None
    ) -> tuple[str | None, bool, Any, Any]:
        """Decrypt a cursor and reject one built for a different sort.

        A malformed or tampered cursor is a client error (``400``); a cursor
        whose ``(sort_field, ascending)`` does not match the request is rejected
        rather than applied against the wrong field.
        """
        try:
            cursor_field, cursor_ascending, sort_key, id_value = decode_cursor(
                self._encryption(), cursor
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc
        attribute = getattr(sort_order, "attribute", None)
        expected_ascending = not bool(getattr(sort_order, "descending", False))
        if cursor_field != attribute or cursor_ascending != expected_ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; "
                "start a new search without a cursor when changing sort."
            )
        return cursor_field, cursor_ascending, sort_key, id_value

    # ------------------------------------------------------------------
    # Item -> DTO projection
    # ------------------------------------------------------------------

    def _to_dto(self, item: Any) -> T:
        """Project a served item into the resource's DTO type.

        The item first passes through the resource's ``clone_for_output`` (a
        defensive resource deep-copies here, so a served object can never be
        mutated through a result). The served Pydantic model *is* the DTO, so a
        non-defensive read returns the stored object itself (v1's
        ``nonDefensiveServesStore``). The field-by-field projection only runs for
        the ``dto=`` escape hatch, where the served model differs from the
        declaration.
        """
        served = self._resource.clone_for_output(item)
        dto_type = self._resource.get_dto_type()
        if isinstance(served, dto_type):
            return served
        values = {name: getattr(served, name, None) for name in dto_type.model_fields}
        return dto_type.model_validate(values)

    def _encryption(self) -> EncryptionService:
        return self._resource._encryption_service


def _ascending_key(value: Any) -> tuple[bool, Any]:
    """A sort key placing ``None`` first ascending without comparing it to values."""
    return (value is not None, value)


def _after_cursor_item(
    key: Any,
    id_value: Any,
    *,
    cursor_key: Any,
    cursor_id: Any,
    ascending: bool,
) -> bool:
    """Whether a row at ``(key, id_value)`` sorts strictly after the cursor row.

    The in-memory mirror of :func:`resourcey.v2.sql.cursor.keyset_predicate`'s
    non-id branch: ``None`` is the null block (first ascending, last
    descending), and only the sort-key comparison mirrors for descending.
    """
    if ascending:
        if cursor_key is None:
            return key is not None or bool(id_value > cursor_id)
        if key is None:
            return False
        return bool(key > cursor_key) or (key == cursor_key and bool(id_value > cursor_id))
    if cursor_key is None:
        return key is None and bool(id_value > cursor_id)
    if key is None:
        return True
    return bool(key < cursor_key) or (key == cursor_key and bool(id_value > cursor_id))
