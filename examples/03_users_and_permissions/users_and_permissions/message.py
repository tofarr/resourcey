"""The ``Message`` resource (example 03).

Mirrors example 01's ``Message`` (``thread_id`` FK to ``threads.id``) but uses
a UUID primary key and adds a ``creator_id`` UUID column so
:class:`~resourcey.auth.permission.CreatorPermission` can scope a principal's
messages to those they created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from resourcey.util.search_filter import BaseSearchFilter
from sqlalchemy import Column, ForeignKey, Uuid


class Message(SqlResource):
    """A message belonging to a thread.

    Fields:
        id: UUID primary key (Python-defaulted ``uuid4``).
        thread_id: FK to ``threads.id`` (one-to-many, required).
        text: The message body (required).
        creator_id: The principal who created the message; auto-stamped from the
            authenticated principal on create.
        created_at / updated_at: Auto-managed timestamps.
    """

    id: Annotated[
        UUID,
        ResourceyField(column=Column("id", Uuid, primary_key=True, default=uuid4, nullable=False)),
    ]
    thread_id: Annotated[
        UUID,
        ResourceyField(
            column=Column("thread_id", Uuid, ForeignKey("threads.id"), nullable=False, index=True)
        ),
    ]
    text: str
    creator_id: Annotated[
        UUID | None,
        ResourceyField(
            column=Column("creator_id", Uuid, ForeignKey("users.id"), nullable=True, index=True)
        ),
    ] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[MessageSearchFilter]:
        """Expose ``thread_id__eq`` so a thread's messages can be listed."""
        return MessageSearchFilter


_MessageModel = Message.get_sql_alchemy_model()


class MessageSearchFilter(BaseSearchFilter[_MessageModel]):  # type: ignore[valid-type]
    """Filter clauses for ``Message.search``."""

    thread_id__eq: UUID | None = None
    text__contains: str | None = None
    creator_id__eq: UUID | None = None
