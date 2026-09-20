"""The ``Thread`` resource — the parent side of the message board (MongoDB).

A thread groups a collection of messages. Unlike the SQL example (where
``Thread`` extends ``SqlResource`` and uses an auto-increment integer id),
this Mongo variant uses a client-generated ``UUID`` id — there is no
auto-increment in MongoDB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field
from resourcey.mongo.mongo_resource import MongoResource


class Thread(MongoResource):
    """A message-board thread backed by a MongoDB collection.

    Fields:
        id: Client-generated UUID primary key (stored as ``_id``).
        title: Short, human-readable title (required, 1-200 chars).
        description: Longer optional body text.
        created_at: Set automatically on create; never updatable.
        updated_at: Refreshed automatically on update; never updatable.
    """

    id: UUID
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
