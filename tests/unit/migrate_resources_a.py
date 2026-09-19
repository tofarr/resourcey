"""Module-level resources for migration tests.

``import_resource_modules`` imports modules by fully-qualified name, so the
resource classes must live at module scope.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from resourcey.resource.base import BaseResource
from resourcey.resource.registry import register_resource


class Widget(BaseResource):
    """A widget with id, label, and a created_at timestamp."""

    id: int
    label: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


register_resource(Widget)
