"""The ``Thread`` resource (example 03).

A message-board thread, identical in shape to example 01's. The example's whole
point is the surrounding API-key security posture, so the resources stay plain
and unencumbered by auth concerns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from resourcey.resource.sql import SqlResource


class Thread(SqlResource):
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
