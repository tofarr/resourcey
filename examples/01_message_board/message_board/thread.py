"""The ``Thread`` resource — the parent side of the message board.

A thread groups a collection of messages. The one-to-many relation to
``Message`` is expressed via ``Message.thread_id`` (a real FK column); this
resource itself declares only the thread's own fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from resourcey.resource.base import BaseResource


class Thread(BaseResource):
    """A message-board thread.

    Fields:
        id: Auto-incrementing primary key.
        title: Short, human-readable title (required, 1-200 chars).
        description: Longer optional body text.
        created_at: Set automatically on create; never creatable/updatable.
        updated_at: Refreshed automatically on update; never creatable/updatable.
    """

    id: int
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
