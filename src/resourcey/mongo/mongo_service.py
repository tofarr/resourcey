"""``MongoService`` — the MongoDB-backed service for a resource.

A :class:`~resourcey.mongo.mongo_resource.MongoResource` subclass yields a
``MongoService`` from :meth:`~resourcey.mongo.mongo_resource.MongoResource.build_service`.
The service holds a ``motor`` async collection as instance state (mirroring how
:class:`~resourcey.resource.service.SqlService` holds an ``AsyncSession``) and
implements the standard :class:`~resourcey.resource.service_base.BaseService`
``Action`` contract against it. The action signatures carry no notion of
session, user, or RBAC — those are instance state, keeping the service
wrappable (issue #40).

A document is the resource's read-model serialization plus the resource's id
field stored under ``_id``. The service translates create/update payloads to
documents, applies keyset cursor pagination (reusing the same opaque/encrypted
cursor encoding as the SQL path), and delegates filter translation to
:mod:`resourcey.mongo.mongo_filter`.

Non-SQL resources do **not** participate in Alembic migrations. Instead,
:meth:`~resourcey.mongo.mongo_resource.MongoResource.migrate_document` is an
opt-in hook (default no-op) invoked on every read so an application can lazily
upgrade a document to the current shape. The versioning scheme is
application-defined.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel

from resourcey.resource.errors import NotFoundError
from resourcey.resource.paged_service import DEFAULT_LIMIT, PagedService
from resourcey.resource.service import Page

if TYPE_CHECKING:
    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


T = TypeVar("T")

# Sentinel for "no default" on a Pydantic field (Pydantic uses a private sentinel
# object; ``None`` is a valid default, so we must distinguish).
_UNDEFINED: Any = object()


class MongoService(PagedService):
    """The MongoDB-backed service exposing the standard resource actions.

    Constructed from a :class:`~resourcey.resource.base.BaseResource` (a
    ``MongoResource`` or a wrapper delegating to one) and an async ``motor``
    collection (instance state, not a per-call parameter). The collection is
    duck-typed: any object exposing the ``motor`` async collection API
    (``insert_one``, ``find_one``, ``find``, ``update_one``, ``delete_one``,
    ``count_documents``, ``replace_one``) works, so tests can substitute an
    in-process adapter backed by ``mongomock``.
    """

    def __init__(
        self,
        resource: BaseResource,
        *,
        collection: Any,
        serialization_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(resource)
        self.create_model = resource.get_create_model()
        self.update_model = resource.get_update_model()
        self.read_model = resource.get_read_model()
        self._collection = collection
        self._serialization_context = serialization_context

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        return self._serialization_context

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: BaseModel) -> Any:
        """Validate via the create model, persist, return the read model (HTTP 201)."""
        doc = self._payload_to_doc(payload, fill_defaults=True)
        await self._collection.insert_one(doc)
        return self._doc_to_read_model(doc)

    async def read(self, id: Any) -> Any:  # noqa: A002
        """Fetch; raise :class:`NotFoundError` (-> 404) if absent."""
        doc = await self._collection.find_one({"_id": _encode_value(id)})
        if doc is None:
            raise NotFoundError(type(self.resource).__name__, id)
        return self._doc_to_read_model(doc)

    async def update(self, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        """Validate via the PATCH update model, apply; raise ``NotFoundError`` if absent."""
        updates = self._payload_to_doc(payload, fill_defaults=False)
        encoded_id = _encode_value(id)
        if updates:
            result = await self._collection.find_one_and_update(
                {"_id": encoded_id},
                {"$set": updates},
                return_document=True,
            )
        else:
            result = await self._collection.find_one({"_id": encoded_id})
        if result is None:
            raise NotFoundError(type(self.resource).__name__, id)
        return self._doc_to_read_model(result)

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete; raise ``NotFoundError`` if absent. Returns no body (HTTP 204)."""
        result = await self._collection.delete_one({"_id": _encode_value(id)})
        if result.deleted_count == 0:
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
        """Search with cursor pagination, sort, and optional filters; return a :class:`Page`."""
        limit = self.validate_limit(limit)
        sort_parsed = self.parse_sort(sort, desc)
        decoded_cursor = self.decode_cursor(cursor, sort_parsed)
        query = to_mongo_query(filters, id_field=self.id_field)
        mongo_sort = self._mongo_sort(sort_parsed)
        cursor_filter = self._cursor_filter(decoded_cursor, sort_parsed)
        if cursor_filter is not None:
            query = _merge_query(query, cursor_filter)
        # Fetch one extra to detect a next page without a separate count.
        result_cursor: Any = self._collection.find(query, limit=limit + 1)
        if mongo_sort:
            result_cursor = result_cursor.sort(mongo_sort)
        docs: list[dict[str, Any]] = await result_cursor.to_list(length=limit + 1)
        has_next = len(docs) > limit
        docs = docs[:limit]
        items = [self._doc_to_read_model(doc) for doc in docs]
        next_cursor = self.next_cursor(items, sort_parsed) if has_next else None
        return Page(items=items, limit=limit, next_cursor=next_cursor)

    async def count(
        self,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        """Return the number of documents matching ``filters``."""
        query = to_mongo_query(filters, id_field=self.id_field)
        return int(await self._collection.count_documents(query or {}))

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        """Return read models positionally aligned with the input ids."""
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        encoded_ids = [_encode_value(i) for i in unique_ids]
        result_cursor: Any = self._collection.find({"_id": {"$in": encoded_ids}})
        docs: list[dict[str, Any]] = await result_cursor.to_list(length=len(unique_ids))
        by_id = {_decode_value(doc["_id"]): doc for doc in docs}
        return [self._doc_to_read_model(by_id[i]) if i in by_id else None for i in ids]

    async def batch_edit(
        self,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        """Apply each edit (id + update payload); return results in input order."""
        results: list[Any] = []
        for edit_id, payload in edits:
            updates = self._payload_to_doc(payload, fill_defaults=False)
            encoded_id = _encode_value(edit_id)
            if updates:
                doc = await self._collection.find_one_and_update(
                    {"_id": encoded_id},
                    {"$set": updates},
                    return_document=True,
                )
            else:
                doc = await self._collection.find_one({"_id": encoded_id})
            results.append(self._doc_to_read_model(doc) if doc is not None else None)
        return results

    # ------------------------------------------------------------------
    # Document <-> model translation
    # ------------------------------------------------------------------

    def _payload_to_doc(
        self,
        payload: BaseModel,
        *,
        fill_defaults: bool,
    ) -> dict[str, Any]:
        """Dump a create/update payload to a Mongo document dict.

        ``exclude_unset=True`` drops fields the caller did not supply (PATCH
        semantics for update). For create, ``fill_defaults=True`` additionally
        populates defaults for non-supplied fields (e.g. ``created_at`` from
        its ``default_factory``). The id field is stored under ``_id`` so Mongo
        indexes it as the primary key; when the client omits it on create, a
        fresh ``UUID`` is generated (client-generated ids, not auto-increment).
        All ``UUID`` values are encoded as strings (see :func:`_encode_value`)
        since bson cannot encode native ``UUID`` without a configured
        ``UuidRepresentation``.
        """
        data = payload.model_dump(context=self._ctx(), exclude_unset=True)
        if fill_defaults:
            for name, field in self.resource.model_fields.items():
                if name in data:
                    continue
                if name == self.id_field:
                    data[self.id_field] = uuid4()
                    continue
                if field.default_factory is not None:
                    factory: Any = field.default_factory
                    data[name] = factory()
                elif field.default is not None and field.default is not _UNDEFINED:
                    data[name] = field.default
        # Encode all UUID values to strings for bson compatibility.
        data = {k: _encode_value(v) for k, v in data.items()}
        # Move the id field to ``_id`` so Mongo indexes it as the primary key.
        if self.id_field in data:
            data["_id"] = data.pop(self.id_field)
        return data

    def _doc_to_read_model(self, doc: dict[str, Any] | None) -> Any:
        """Project a Mongo document into the resource's read-model instance.

        Invokes the resource's ``migrate_document`` hook first (default no-op)
        so an application can lazily upgrade a document on read. Maps ``_id``
        back to the resource's id field name, decodes string-encoded UUIDs
        back to ``UUID`` objects, then validates into the read model with the
        serialization context (so secret fields decrypt).
        """
        if doc is None:
            return None
        migrated = self.resource.migrate_document(doc)
        data = dict(migrated)
        if "_id" in data:
            data[self.id_field] = _decode_value(data.pop("_id"))
        # Decode string-encoded UUIDs back to UUID objects for fields whose
        # annotation is UUID (bson cannot store native UUID without a configured
        # UuidRepresentation, so all UUIDs are stored as strings).
        for name, field in self.read_model.model_fields.items():
            if name in data and data[name] is not None and _field_is_uuid(field):
                data[name] = _decode_value(data[name])
        names = self.read_model.model_fields
        projected = {name: data.get(name) for name in names}
        return self.read_model.model_validate(projected, context=self._ctx())

    # ------------------------------------------------------------------
    # Mongo-specific sort + cursor translation
    # ------------------------------------------------------------------

    def _mongo_sort(self, sort_parsed: tuple[str, bool] | None) -> list[tuple[str, int]] | None:
        """Translate the validated sort into a Mongo sort spec (``[(field, 1|-1)]``)."""
        if sort_parsed is None:
            # Default: order by ``_id`` ascending so pagination is stable.
            return [("_id", 1)]
        field, ascending = sort_parsed
        direction = 1 if ascending else -1
        mongo_field = "_id" if field == self.id_field else field
        return [(mongo_field, direction), ("_id", direction)]

    def _cursor_filter(
        self,
        decoded_cursor: tuple[Any, Any] | None,
        sort_parsed: tuple[str, bool] | None,
    ) -> dict[str, Any] | None:
        """Build the keyset ``$gt``/``$lt`` filter that seeks past the cursor row.

        Mirrors :func:`resourcey.resource.cursor.keyset_predicate`: for ascending
        order, keep documents where ``(sort_key, _id) > (cursor_key, cursor_id)``;
        for descending, mirror the comparison. When the sort field is the id
        (the default no-sort case), the predicate collapses to a single ``_id``
        comparison. Cursor values are encoded via :func:`_encode_value` so a
        ``UUID`` id compares against its stored string form.
        """
        if decoded_cursor is None:
            return None
        cursor_key, cursor_id = decoded_cursor
        encoded_key = _encode_value(cursor_key)
        encoded_id = _encode_value(cursor_id)
        ascending = sort_parsed[1] if sort_parsed is not None else True
        sort_field = self.sort_key_field(sort_parsed)
        op = "$gt" if ascending else "$lt"
        if sort_field == self.id_field:
            return {"_id": {op: encoded_id}}
        return {
            "$or": [
                {sort_field: {op: encoded_key}},
                {sort_field: encoded_key, "_id": {op: encoded_id}},
            ]
        }


def _encode_value(value: Any) -> Any:
    """Encode a value for Mongo storage (UUID -> string, others passthrough).

    ``uuid.UUID`` cannot be stored directly unless the client is configured
    with a ``UuidRepresentation`` (bson raises by default). Storing the string
    form is portable across mongomock and real MongoDB, and the string
    round-trips back to a ``UUID`` via :func:`_decode_value`.
    """
    if isinstance(value, UUID):
        return str(value)
    return value


def _decode_value(value: Any) -> Any:
    """Decode a stored value back to its native Python type.

    Reconstructs a ``UUID`` from its string form. Other types pass through.
    """
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return value
    return value


def _field_is_uuid(field: Any) -> bool:
    """Check whether a Pydantic field's annotation is (or contains) ``UUID``."""
    import typing
    from uuid import UUID as _UUID

    ann = field.annotation
    if ann is _UUID:
        return True
    # Handle ``UUID | None`` (Union) annotations.
    origin = typing.get_origin(ann)
    if origin is not None:
        args = typing.get_args(ann)
        return any(a is _UUID for a in args)
    return False


def _merge_query(
    base: dict[str, Any] | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Merge two Mongo query dicts into a single ``$and`` query.

    Keeps both the filter predicates and the keyset cursor predicate. When
    ``base`` is ``None`` (no filter), ``extra`` is the whole query.
    """
    if base is None:
        return extra
    return {"$and": [base, extra]}


# Imported at the bottom to avoid a circular import at module load
# (mongo_filter imports nothing from mongo_service).
from resourcey.mongo.mongo_filter import to_mongo_query  # noqa: E402
