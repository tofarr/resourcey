"""Embedded async Mongo client for tests and local development.

``motor`` cannot wrap a ``mongomock`` client directly (it interprets the
client as a host string). This module provides ``AsyncEmbeddedClient`` — an
async client backed by an in-process ``mongomock`` server that exposes the
subset of the ``motor`` client/database/collection API that
:class:`~resourcey.mongo.mongo_service.MongoService` uses.

Use it as the ``client`` for
:meth:`~resourcey.mongo.mongo_resource.MongoResource.configure` when you want
an embedded, zero-dependency Mongo for tests or local development::

    from resourcey.mongo.embedded import AsyncEmbeddedClient

    client = AsyncEmbeddedClient()
    MyResource.configure(client=client, database_name="app")

No external server is started. The client is not a real ``motor`` client, but
it satisfies the duck-typed interface the service expects.
"""

from __future__ import annotations

from typing import Any

import mongomock
from pymongo import ReturnDocument
from pymongo.results import DeleteResult, InsertOneResult, UpdateResult


class _EmbeddedCursor:
    """Async cursor over a list of docs, with ``.sort()`` and ``.to_list()``."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._limit = 0

    def sort(self, key_or_list: Any) -> _EmbeddedCursor:
        if not key_or_list:
            return self
        specs = key_or_list if isinstance(key_or_list, list) else [key_or_list]
        fields = [(f, d) for f, d in specs]

        def _key(doc: dict[str, Any]) -> tuple[Any, ...]:
            return tuple(_coerce(doc.get(field)) for field, _ in fields)

        self._docs = sorted(self._docs, key=_key)
        if any(d == -1 for _, d in fields):
            self._docs.reverse()
        return self

    def limit(self, n: int) -> _EmbeddedCursor:
        self._limit = n
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = self._docs
        lim = length if length is not None else self._limit
        if lim:
            docs = docs[:lim]
        return [dict(d) for d in docs]


def _coerce(value: Any) -> Any:
    """Sort key coercion: ``None`` sorts before everything (Mongo semantics)."""
    if value is None:
        return (0, "")
    return (1, value)


class _EmbeddedCollection:
    """Async adapter over a ``mongomock`` collection, matching motor's API."""

    def __init__(self, collection: Any) -> None:
        self._col = collection

    async def insert_one(self, doc: dict[str, Any]) -> InsertOneResult:
        result = self._col.insert_one(doc)
        return InsertOneResult(result.inserted_id, acknowledged=True)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self._col.find_one(query)
        return result

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: bool = True,
    ) -> dict[str, Any] | None:
        doc = self._col.find_one_and_update(
            query,
            update,
            return_document=ReturnDocument.AFTER if return_document else ReturnDocument.BEFORE,
        )
        return doc if doc is None else dict(doc)

    def find(self, query: dict[str, Any] | None = None, *, limit: int = 0) -> _EmbeddedCursor:
        docs = list(self._col.find(query or {}))
        cursor = _EmbeddedCursor(docs)
        if limit:
            cursor._limit = limit
        return cursor

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> UpdateResult:
        result = self._col.update_one(query, update)
        return UpdateResult(result.raw_result, acknowledged=True)

    async def delete_one(self, query: dict[str, Any]) -> DeleteResult:
        result = self._col.delete_one(query)
        return DeleteResult(result.raw_result, acknowledged=True)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return int(self._col.count_documents(query))

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        return str(self._col.create_index(keys, **kwargs))


class _EmbeddedDatabase:
    """Async database wrapper producing :class:`_EmbeddedCollection` objects."""

    def __init__(self, database: Any) -> None:
        self._db = database

    def __getitem__(self, name: str) -> _EmbeddedCollection:
        return _EmbeddedCollection(self._db[name])


class AsyncEmbeddedClient:
    """Embedded async Mongo client backed by ``mongomock``.

    Drop-in for ``motor.motor_asyncio.AsyncIOMotorClient`` in tests and local
    development. Exposes ``__getitem__`` to access a database, which in turn
    exposes ``__getitem__`` to access a collection — matching motor's API.
    """

    def __init__(self, *, tz_aware: bool = True) -> None:
        self._client: Any = mongomock.MongoClient(tz_aware=tz_aware)

    def __getitem__(self, name: str) -> _EmbeddedDatabase:
        return _EmbeddedDatabase(self._client[name])

    def close(self) -> None:
        self._client.close()
