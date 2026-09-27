"""The ``Message`` DTO + resource — the child side of the message board (MongoDB).

A message belongs to a single thread via ``thread_id`` (a UUID). Unlike the SQL
example (which uses a real foreign-key column), the Mongo variant stores
``thread_id`` as a plain scalar field — MongoDB has no server-side FK
constraints, so the relation is enforced at the application level. ``v2``'s
projection is plain-columns-only (relationship / nested projection is a known
limitation), which is exactly what this example needs.

The query surface is the read model: a field is filterable / sortable exactly
when the DTO exposes it, so ``GET /messages?thread_id__eq=<id>`` lists a
thread's messages and ``?text__contains=`` does a substring search on the body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from resourcey.v2.core.dto import DTO
from resourcey.v2.mongo.mongo_resource import MongoResource


class MessageDTO(DTO):
    """A message belonging to a thread.

    Fields:
        id: Client-supplied UUID primary key (stored under Mongo's ``_id``).
        thread_id: UUID of the parent thread (required, application-level FK).
        text: The message body (required).
        created_at: Framework-owned; set once on create.
        updated_at: Framework-owned; refreshed on every update.
    """

    id: UUID
    thread_id: UUID
    text: str
    created_at: datetime
    updated_at: datetime


class MessageResource(MongoResource[MessageDTO, UUID]):
    """``MessageDTO`` served from the ``messages`` collection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("path", "messages")
        kwargs.setdefault("collection_name", "messages")
        super().__init__(*args, **kwargs)
