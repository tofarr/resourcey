"""The ``Message`` resource (example 03).

A message belongs to a ``Thread`` via ``thread_id``, a real foreign-key column
to ``threads.id``. A ``MessageSearchFilter`` declares ``thread_id__eq`` so a
client can list a thread's messages via ``GET /messages?thread_id__eq=<id>``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from resourcey.util.search_filter import BaseSearchFilter
from sqlalchemy import Column, ForeignKey, Integer


class Message(SqlResource):
    """A message belonging to a thread.

    Fields:
        id: Auto-incrementing primary key.
        thread_id: FK to ``threads.id`` (one-to-many, required).
        text: The message body (required).
        created_at: Set automatically on create; never creatable/updatable.
        updated_at: Refreshed automatically on update; never creatable/updatable.
    """

    id: int
    thread_id: Annotated[
        int,
        ResourceyField(
            column=Column(
                "thread_id", Integer, ForeignKey("threads.id"), nullable=False, index=True
            )
        ),
    ]
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[MessageSearchFilter]:
        """Expose ``thread_id__eq`` so a thread's messages can be listed."""
        return MessageSearchFilter


_MessageModel = Message.get_sql_alchemy_model()


class MessageSearchFilter(BaseSearchFilter[_MessageModel]):  # type: ignore[valid-type]
    """Filter clauses for ``Message.search``.

    ``thread_id__eq`` lists a thread's messages; ``text__contains`` supports
    substring search on the body.
    """

    thread_id__eq: int | None = None
    text__contains: str | None = None
