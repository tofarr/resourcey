"""Module-level resources for migration tests.

``import_resource_modules`` imports modules by fully-qualified name, so the
resource classes must live at module scope.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from resourcey.resource.sql import SqlResource


class Widget(SqlResource):
    """A widget with id, label, and a created_at timestamp."""

    id: int
    label: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
