"""Async adapter wrapping a ``mongomock`` collection for unit tests.

``motor`` cannot wrap a ``mongomock`` client directly (it interprets the
client as a host). This module re-exports
:class:`~resourcey.mongo.embedded.AsyncEmbeddedClient` for unit-test
convenience and provides a helper to build a standalone
:class:`AsyncMockCollection` when a test needs direct access to the
collection without going through the client/database indirection.
"""

from __future__ import annotations

from typing import Any

import mongomock
from pymongo import ReturnDocument
from pymongo.results import DeleteResult, InsertOneResult, UpdateResult

from resourcey.mongo.embedded import AsyncEmbeddedClient


class AsyncMockCursor:
    """Async cursor over a list of docs, with ``.sort()`` and ``.to_list()``."""

    _pending_limit: int = 0

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._pending_limit = 0

    def sort(self, key_or_list: Any) -> AsyncMockCursor:
        if not key_or_list:
            return self
        specs = key_or_list if isinstance(key_or_list, list) else [key_or_list]
        fields = [(f, d) for f, d in specs]

        def _sort_key(doc: dict[str, Any]) -> tuple[Any, ...]:
            return tuple(_coerce(doc.get(field)) for field, _ in fields)

        self._docs = sorted(self._docs, key=_sort_key)
        if any(d == -1 for _, d in fields):
            self._docs.reverse()
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = self._docs
        limit = length if length is not None else self._pending_limit
        if limit:
            docs = docs[:limit]
        return [dict(d) for d in docs]


def _coerce(value: Any) -> Any:
    """Sort key coercion: ``None`` sorts before everything (Mongo semantics)."""
    if value is None:
        return (0, "")
    return (1, value)


class AsyncMockCollection:
    """Async adapter over a ``mongomock`` collection, matching motor's API."""

    def __init__(self, collection: Any) -> None:
        self._col = collection

    async def insert_one(self, doc: dict[str, Any]) -> InsertOneResult:
        result = self._col.insert_one(doc)
        return InsertOneResult(result.inserted_id, acknowledged=True)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self._col.find_one(query)

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
        return doc

    def find(self, query: dict[str, Any] | None = None, *, limit: int = 0) -> AsyncMockCursor:
        # Return all matching docs; the cursor's ``sort`` is applied before
        # ``to_list`` slices to ``limit``, so sort-then-limit ordering is correct.
        docs = list(self._col.find(query or {}))
        cursor = AsyncMockCursor(docs)
        cursor._pending_limit = limit  # type: ignore[attr-defined]
        return cursor

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> UpdateResult:
        result = self._col.update_one(query, update)
        return UpdateResult(result.raw_result, acknowledged=True)

    async def delete_one(self, query: dict[str, Any]) -> DeleteResult:
        result = self._col.delete_one(query)
        return DeleteResult(result.raw_result, acknowledged=True)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return self._col.count_documents(query)

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        return self._col.create_index(keys, **kwargs)


def make_mock_client() -> AsyncEmbeddedClient:
    """Build an :class:`AsyncEmbeddedClient` (use in tests as the ``client`` for configure)."""
    return AsyncEmbeddedClient()


def mock_collection(database_name: str, collection_name: str) -> AsyncMockCollection:
    """Build an :class:`AsyncMockCollection` for a fresh in-memory database."""
    client = mongomock.MongoClient(tz_aware=True)
    return AsyncMockCollection(client[database_name][collection_name])
