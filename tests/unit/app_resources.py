"""Module-level resource classes for ``create_app`` / CLI tests.

``create_app``'s config-driven ``resources`` resolution imports the named
classes by fully-qualified dotted path, so the classes must live at module
scope (not local to a test function) to be importable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from resourcey.resource.base import BaseResource


class AppWidget(BaseResource):
    """A simple resource for ``create_app`` integration tests — int id + label."""

    id: int
    label: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppGadget(BaseResource):
    """A second resource so multi-resource registration is exercised."""

    id: int
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Resolve ORM models eagerly so metadata is populated before table creation.
for _r in (AppWidget, AppGadget):
    _r.get_sql_alchemy_model()
