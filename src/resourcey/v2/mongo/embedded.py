"""Embedded async Mongo client for tests and local development (issue #80).

``motor`` cannot wrap a ``mongomock`` client directly (it interprets the client
as a host string). This module provides :class:`AsyncEmbeddedClient` — an async
client backed by an in-process ``mongomock`` server that exposes the subset of
the ``motor`` client/database/collection API that
:class:`~resourcey.v2.mongo.mongo_service.MongoService` uses.

It is selected by a connection URL of ``embedded://<db>`` (or the bare
``embedded`` marker), so the example and the tests need no external server. No
external process is started; the client is not a real ``motor`` client, but it
satisfies the duck-typed interface the service expects.

The cursor delegates ``sort`` / ``limit`` to the underlying ``mongomock`` cursor
rather than re-sorting in Python, so Mongo's per-type NULL ordering (NULLs first
ascending, last descending) and BSON comparison rules are reproduced exactly —
which is what the keyset predicate and the in-memory ``compare`` reference must
agree with.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from typing import Any

import mongomock
from pymongo import ReturnDocument
from pymongo.results import DeleteResult, InsertOneResult, UpdateResult


class _EmbeddedCursor:
    """Async cursor over a ``mongomock`` cursor, with ``sort`` / ``limit`` / ``to_list``."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def sort(self, key_or_list: Any) -> _EmbeddedCursor:
        self._cursor = self._cursor.sort(key_or_list)
        return self

    def limit(self, n: int) -> _EmbeddedCursor:
        self._cursor = self._cursor.limit(n)
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = [dict(doc) for doc in self._cursor]
        if length is not None:
            docs = docs[:length]
        return docs


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
        cursor = _EmbeddedCursor(self._col.find(query or {}))
        if limit:
            cursor.limit(limit)
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
