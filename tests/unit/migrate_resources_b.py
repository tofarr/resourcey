"""A second module-level resource for migration tests.

``Gadget`` is a distinct resource (different class/table name) so it can be
imported alongside ``migrate_resources_a.Widget`` without colliding in
SQLAlchemy's class registry.
"""

from __future__ import annotations

from resourcey.resource.sql import SqlResource


class Gadget(SqlResource):
    """A gadget with an id and a name."""

    id: int
    name: str
