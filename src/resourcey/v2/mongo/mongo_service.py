"""``MongoService`` — the action layer of the ``v2`` Mongo backend (issue #80).

Implements the eight standard actions against a duck-typed async collection
(``insert_one``, ``find_one``, ``find``, ``find_one_and_update``, ``delete_one``,
``count_documents``, ``create_index``) so ``mongomock`` substitutes for ``motor``
in tests. A document is the DTO's fields, with the identifier stored under
Mongo's ``_id``; ``UUID`` values are stored as their string form (bson cannot
encode a native ``UUID`` without a configured representation) and coerced back
by Pydantic on read.

``search`` implements keyset (seek) cursor pagination ordered by the identifier
by default, or by a validated ``sort`` field with ``_id`` appended as a stable
tie-breaker. The cursor is the shared, tamper-proof
:mod:`resourcey.v2.util.cursor` codec, so a cursor reused under a different sort
is rejected rather than applied against the wrong field. Mongo's fixed NULL
ordering (nulls first ascending, last descending) is what the in-memory
:meth:`AttrSortOrder.compare` reference expects, so the keyset predicate needs no
NULL special-casing beyond that.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from resourcey.v2.core.dto import apply_operation_defaults
from resourcey.v2.core.errors import InvalidInputError, UnsupportedFilterError
from resourcey.v2.core.service import (
    DEFAULT_LIMIT,
    STORAGE_KEY,
    Action,
    Create,
    Delete,
    NotFoundError,
    Page,
    Service,
    ServiceError,
    Update,
)
from resourcey.v2.util.cursor import decode_cursor, encode_cursor
from resourcey.v2.util.missing import MISSING
from resourcey.v2.util.search_filter import SearchFilter
from resourcey.v2.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.v2.encryption.encryption_service import EncryptionService
    from resourcey.v2.mongo.mongo_resource import MongoResource

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")

# A Mongo query dict (``None`` = no restriction) and sort spec, both as ``motor`` expects.
Query = dict[str, Any] | None
SortSpec = list[tuple[str, int]]


def _encode_value(value: Any) -> Any:
    """Encode a value for storage / querying (``UUID`` -> string, datetimes to UTC)."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


class MongoService(Service[T, K]):
    """The standard actions over a Mongo collection, with keyset cursor pagination.

    Holds the call-scoped ``ctx`` and the collection (instance state, not a
    per-call parameter). Motor manages its own connection pool, so — unlike the
    SQL service — there is no per-request session to commit or close: each
    operation is atomic at the document level. The ``ctx`` is still adopted and
    exposed so a caller can share call-scoped state, matching the SQL seam.
    """

    def __init__(
        self,
        resource: MongoResource[T, K],
        ctx: MutableMapping[Any, Any],
        collection: Any,
    ) -> None:
        super().__init__()
        self._resource = resource
        self._ctx = ctx
        self._collection = collection

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> MongoService[T, K]:
        await super().__aenter__()
        self._ctx.setdefault(STORAGE_KEY, self._collection)
        return self

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        """Insert a DTO, filling omitted fields from create defaults; return the DTO."""
        data = apply_operation_defaults(
            self._resource.get_dto_declaration(), _payload_values(payload), "create"
        )
        document = self._to_document(data)
        result = await self._collection.insert_one(document)
        document.setdefault("_id", getattr(result, "inserted_id", None))
        return self._from_document(document)

    async def read(self, id: K) -> T:  # noqa: A002
        """Fetch one DTO by id; raise :class:`NotFoundError` if absent."""
        document = await self._collection.find_one({"_id": _encode_value(id)})
        if document is None:
            raise NotFoundError(id)
        return self._from_document(document)

    async def update(self, payload: T) -> T:
        """Apply an update DTO (whose id field names the document); return the DTO.

        Supplied (non-``MISSING``) fields are written; omitted fields with an
        update default take it, and an omitted field with no update default is
        left unchanged (PATCH semantics). The id is never written.
        """
        id_field = self._resource.get_id_field()
        values = _payload_values(payload)
        id_value = values.get(id_field)
        if id_value is MISSING or id_value is None:
            raise ServiceError("update requires the identifier on the payload")
        data = {k: v for k, v in values.items() if k != id_field}
        data = apply_operation_defaults(self._resource.get_dto_declaration(), data, "update")
        updates = self._to_document(data)
        if updates:
            document = await self._collection.find_one_and_update(
                {"_id": _encode_value(id_value)},
                {"$set": updates},
                return_document=True,
            )
        else:
            document = await self._collection.find_one({"_id": _encode_value(id_value)})
        if document is None:
            raise NotFoundError(id_value)
        return self._from_document(document)

    async def delete(self, id: K) -> None:  # noqa: A002
        """Delete by id; raise :class:`NotFoundError` if absent."""
        result = await self._collection.delete_one({"_id": _encode_value(id)})
        if result.deleted_count == 0:
            raise NotFoundError(id)

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        """Return up to ``limit`` documents, with keyset pagination via ``cursor``.

        ``sort_order`` is the validated ordering (``None`` for the default
        identifier order); ``_id`` is appended as a stable tie-breaker by the
        sort converter. ``search_filter`` is a standard :class:`SearchFilter`
        tree (an object filter is lowered first) and is pushed into the query
        before the page is taken. The next cursor is derived from the *same*
        ``sort_order`` the page was ordered and sought by, so it can never be
        rejected by the predicate that follows it.
        """
        query = await self._filter_query(search_filter)
        if query is _UNCONVERTIBLE:
            return await self._search_by_iteration(search_filter, sort_order, cursor, limit)
        predicate = self._cursor_predicate(cursor, sort_order)
        query = _merge_query(cast("Query", query), predicate)
        result = self._collection.find(query)
        sort_spec = self._sort_spec(sort_order)
        if sort_spec:
            result = result.sort(sort_spec)
        documents: list[dict[str, Any]] = await result.to_list(length=limit + 1)
        has_more = len(documents) > limit
        page_documents = documents[:limit]
        next_cursor = (
            self._next_cursor(page_documents[-1], sort_order)
            if has_more and page_documents
            else None
        )
        return Page(
            items=[self._from_document(doc) for doc in page_documents],
            limit=limit,
            next_cursor=next_cursor,
        )

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        """Return the number of documents matching ``search_filter`` (all when ``None``)."""
        query = await self._filter_query(search_filter)
        if query is _UNCONVERTIBLE:
            return len(await self._iterated_documents(search_filter))
        return int(await self._collection.count_documents(cast("Query", query) or {}))

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        """Return DTOs positionally aligned with ``ids`` (``None`` for absent)."""
        if not ids:
            return []
        unique = list(dict.fromkeys(ids))
        encoded = [_encode_value(i) for i in unique]
        result = self._collection.find({"_id": {"$in": encoded}})
        documents: list[dict[str, Any]] = await result.to_list(length=len(unique))
        by_id = {doc["_id"]: self._from_document(doc) for doc in documents}
        return [by_id.get(_encode_value(i)) for i in ids]

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Apply each :class:`Edit`; results align positionally with ``edits``.

        A create / update yields the resulting DTO, a delete yields ``None``
        (nothing to return), and a miss (an absent id on update / delete) also
        yields ``None``.

        A create / delete is refused with :class:`InvalidInputError` unless the
        resource *declares* the matching action, so a batch can never reach an
        action the resource does not expose (the transport narrows the body the
        same way; this is the backend's own guard for direct service callers).
        """
        supported = self._resource.get_supported_actions()
        results: list[T | None] = []
        for edit in edits:
            if isinstance(edit, Create):
                if Action.CREATE not in supported:
                    raise InvalidInputError("batch_edit cannot create: create is not supported")
                results.append(await self.create(edit.item))
            elif isinstance(edit, Update):
                results.append(await self._update_or_none(edit.item))
            else:
                if Action.DELETE not in supported:
                    raise InvalidInputError("batch_edit cannot delete: delete is not supported")
                await self._delete_or_none(edit.id)
                results.append(None)
        return results

    async def _update_or_none(self, payload: T) -> T | None:
        """``update`` but an absent id yields ``None`` instead of raising."""
        try:
            return await self.update(payload)
        except NotFoundError:
            return None

    async def _delete_or_none(self, id: K) -> None:  # noqa: A002
        """``delete`` but an absent id is a no-op instead of raising."""
        try:
            await self.delete(id)
        except NotFoundError:
            return

    # ------------------------------------------------------------------
    # Filter pushdown
    # ------------------------------------------------------------------

    async def _filter_query(self, search_filter: SearchFilter[Any] | None) -> Any:
        """Translate ``search_filter`` into a Mongo query (all-or-nothing).

        Returns ``None`` for no restriction, a query dict, or the
        :data:`_UNCONVERTIBLE` sentinel when the filter cannot be pushed down and
        the resource has opted into the in-memory iteration fallback. An
        unconvertible filter without that opt-in raises
        :class:`UnsupportedFilterError`, since silently scanning every document
        behind a public ``GET`` is a DoS.
        """
        if search_filter is None:
            return None
        standard = search_filter.create_standard_filter()
        converter = self._resource.build_filter_converter()
        try:
            await converter.resolve()
            return converter.apply(standard)
        except UnsupportedFilterError:
            if not self._resource.allow_filter_iteration:
                raise
            return _UNCONVERTIBLE

    async def _iterated_documents(self, search_filter: SearchFilter[Any] | None) -> list[Any]:
        """The opt-in fallback: every document matching ``search_filter`` in memory."""
        standard = search_filter.create_standard_filter() if search_filter is not None else None
        dto_type = self._resource.get_dto_type()
        result = self._collection.find(None)
        documents: list[dict[str, Any]] = await result.to_list(length=None)
        if standard is None:
            return documents
        return [
            doc
            for doc in documents
            if standard.matches(dto_type.model_validate(self._raw_dto(doc)))
        ]

    async def _search_by_iteration(
        self,
        search_filter: SearchFilter[Any] | None,
        sort_order: SortOrder[Any] | None,
        cursor: str | None,
        limit: int,
    ) -> Page[T]:
        """The opt-in fallback: materialise matching ids, then page within them."""
        matching = await self._iterated_documents(search_filter)
        ids = [doc["_id"] for doc in matching]
        predicate = self._cursor_predicate(cursor, sort_order)
        query: Query = {"_id": {"$in": ids}}
        query = _merge_query(query, predicate)
        result = self._collection.find(query)
        sort_spec = self._sort_spec(sort_order)
        if sort_spec:
            result = result.sort(sort_spec)
        documents: list[dict[str, Any]] = await result.to_list(length=limit + 1)
        has_more = len(documents) > limit
        page_documents = documents[:limit]
        next_cursor = (
            self._next_cursor(page_documents[-1], sort_order)
            if has_more and page_documents
            else None
        )
        return Page(
            items=[self._from_document(doc) for doc in page_documents],
            limit=limit,
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # Cursor + sort helpers
    # ------------------------------------------------------------------

    def _encryption(self) -> EncryptionService:
        return self._resource._encryption_service

    def _sort_spec(self, sort_order: SortOrder[Any] | None) -> SortSpec:
        """The Mongo sort spec (``_id`` ascending is the default order)."""
        if sort_order is None:
            return [("_id", 1)]
        return self._resource.build_sort_converter().apply(sort_order)

    def _cursor_predicate(self, cursor: str | None, sort_order: SortOrder[Any] | None) -> Query:
        """The keyset query for ``cursor``, bound to the request's sort.

        A cursor built for a different ``(sort field, direction)`` is rejected
        rather than applied against the wrong field. The sort-order fields
        (``attribute`` / ``descending``) are read through ``getattr`` so a
        declared non-``AttrSortOrder`` node that carries them still binds.
        """
        if cursor is None:
            return None
        cursor_field, cursor_ascending, sort_key, id_value = self._decode(cursor)
        attribute = getattr(sort_order, "attribute", None)
        expected_ascending = not bool(getattr(sort_order, "descending", False))
        if cursor_field != attribute or cursor_ascending != expected_ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; "
                "start a new search without a cursor when changing sort."
            )
        if sort_order is None:
            return {"_id": {"$gt": _encode_value(id_value)}}
        field = self._resource.mongo_field_for(cast("str", attribute))
        return _keyset_query(
            field, _encode_value(sort_key), _encode_value(id_value), cursor_ascending
        )

    def _decode(self, cursor: str) -> tuple[str | None, bool, Any, Any]:
        """Decrypt and validate a cursor, mapping malformed input to a 400."""
        try:
            return decode_cursor(self._encryption(), cursor)
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc

    def _next_cursor(self, document: dict[str, Any], sort_order: SortOrder[Any] | None) -> str:
        """Encode the cursor pointing past ``document`` under the resolved ``sort_order``."""
        id_value = document["_id"]
        attribute = getattr(sort_order, "attribute", None)
        sort_field = attribute if attribute is not None else None
        ascending = not bool(getattr(sort_order, "descending", False))
        if attribute is None:
            sort_key = id_value
        else:
            sort_key = document.get(self._resource.mongo_field_for(attribute))
        return encode_cursor(
            self._encryption(),
            sort_field=sort_field,
            ascending=ascending,
            sort_key=sort_key,
            id_value=id_value,
        )

    # ------------------------------------------------------------------
    # Document <-> DTO projection
    # ------------------------------------------------------------------

    def _to_document(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rename DTO field keys to Mongo field names and encode their values."""
        return {
            self._resource.mongo_field_for(name): _encode_value(value)
            for name, value in data.items()
        }

    def _raw_dto(self, document: dict[str, Any]) -> dict[str, Any]:
        """A document's fields keyed by DTO attribute name (values left encoded)."""
        id_field = self._resource.get_id_field()
        values: dict[str, Any] = {}
        for name in self._resource.get_dto_declaration().__dto_fields__:
            mongo_field = self._resource.mongo_field_for(name)
            if mongo_field in document:
                values[name] = document[mongo_field]
        if "_id" in document:
            values[id_field] = document["_id"]
        return values

    def _from_document(self, document: dict[str, Any]) -> T:
        """Project a Mongo document into a DTO, running the migration-on-read hook."""
        migrated = self._resource.migrate_document(dict(document))
        return self._resource.get_dto_type().model_validate(self._raw_dto(migrated))


def _payload_values(payload: Any) -> dict[str, Any]:
    """The explicitly supplied (non-``MISSING``) fields of a DTO instance."""
    return {name: value for name, value in payload.__dict__.items() if value is not MISSING}


def _merge_query(base: Query, extra: Query) -> Query:
    """Merge two Mongo query dicts, preserving both as an ``$and``."""
    if base is None:
        return extra
    if extra is None:
        return base
    return {"$and": [base, extra]}


def _keyset_query(field: str, cursor_key: Any, cursor_id: Any, ascending: bool) -> Query:
    """Build the Mongo query that seeks past the cursor document.

    Ordering is ``(sort_key, _id)`` with the sort key in the requested direction
    and ``_id`` *always ascending* (the tie-breaker the sort converter appends).
    Ascending keeps ``sort_key > cursor_key``, or an equal key with a greater
    ``_id``; descending keeps ``sort_key < cursor_key``, or an equal key with a
    greater ``_id`` — only the sort-key comparison mirrors, never the tie-breaker.

    Mongo places ``null`` first ascending and last descending (the ordering
    :meth:`AttrSortOrder.compare` expects), so a ``None`` cursor key sits in the
    null block: ascending rows after it are the non-null rows then a greater
    ``_id`` within the null block; descending rows after it are only a greater
    ``_id`` within the null block, and a non-null key additionally keeps the
    null block (which sorts last).
    """
    if ascending:
        if cursor_key is None:
            return {"$or": [{field: {"$ne": None}}, {field: None, "_id": {"$gt": cursor_id}}]}
        return {
            "$or": [
                {field: {"$gt": cursor_key}},
                {field: cursor_key, "_id": {"$gt": cursor_id}},
            ]
        }
    if cursor_key is None:
        return {field: None, "_id": {"$gt": cursor_id}}
    return {
        "$or": [
            {field: {"$lt": cursor_key}},
            {field: cursor_key, "_id": {"$gt": cursor_id}},
            {field: None},
        ]
    }


# Sentinel returned by ``_filter_query`` when a filter is unconvertible and the
# resource opted into the in-memory iteration fallback (a query dict is never
# this object, so it is unambiguous).
_UNCONVERTIBLE: Any = object()
