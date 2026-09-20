"""The ``Message`` resource — the child side of the message board (MongoDB).

A message belongs to a single ``Thread`` via ``thread_id`` (a UUID). Unlike
the SQL example (which uses a real foreign-key column), the Mongo variant
stores ``thread_id`` as a plain field — MongoDB has no server-side FK
constraints. The relation is enforced at the application level.

A ``MessageSearchFilter`` declares ``thread_id__eq`` so a client can list a
thread's messages via ``GET /messages?thread_id__eq=<id>``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import Field
from resourcey.mongo.mongo_resource import MongoResource
from resourcey.util.search_filter import BaseSearchFilter


class Message(MongoResource):
    """A message belonging to a thread.

    Fields:
        id: Client-generated UUID primary key (stored as ``_id``).
        thread_id: UUID of the parent thread (required, application-level FK).
        text: The message body (required).
        created_at: Set automatically on create; never updatable.
        updated_at: Refreshed automatically on update; never updatable.
    """

    id: UUID
    thread_id: UUID
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[MessageSearchFilter]:
        return MessageSearchFilter


class MessageSearchFilter(BaseSearchFilter[Any]):
    """Optional filter clauses for ``Message.search``.

    Declares ``thread_id__eq`` so ``GET /messages?thread_id__eq=<id>`` lists a
    thread's messages. ``text__contains`` supports substring search on the body.
    """

    thread_id__eq: UUID | None = None
    text__contains: str | None = None
