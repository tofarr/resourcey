"""The ``Thread`` resource (example 03).

Mirrors example 01's ``Thread`` but uses a UUID primary key and adds a
``creator_id`` UUID column (auto-stamped from the authenticated principal on
create) so :class:`~resourcey.auth.permission.CreatorPermission` can scope a
principal's threads to those they created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from sqlalchemy import Column, ForeignKey, Uuid


class Thread(SqlResource):
    """A message-board thread.

    Fields:
        id: UUID primary key (Python-defaulted ``uuid4``).
        title: Short, human-readable title (required, 1-200 chars).
        description: Longer optional body text.
        creator_id: The principal who created the thread; auto-stamped from the
            authenticated principal on create.
        created_at / updated_at: Auto-managed timestamps.
    """

    id: Annotated[
        UUID,
        ResourceyField(column=Column("id", Uuid, primary_key=True, default=uuid4, nullable=False)),
    ]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = ""
    creator_id: Annotated[
        UUID | None,
        ResourceyField(
            column=Column("creator_id", Uuid, ForeignKey("users.id"), nullable=True, index=True)
        ),
    ] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_ThreadModel = Thread.get_sql_alchemy_model()
