"""A second module-level resource for migration tests.

``Gadget`` is a distinct resource (different class/table name) so it can be
imported alongside ``migrate_resources_a.Widget`` without colliding in
SQLAlchemy's class registry — used to verify the sequential-index
``RESOURCEY_MIGRATION_RESOURCE_MODULES_*`` env parsing path imports multiple
modules and materialises both tables.
"""

from __future__ import annotations

from resourcey.resource.base import BaseResource
from resourcey.resource.registry import register_resource


class Gadget(BaseResource):
    """A gadget with an id and a name."""

    id: int
    name: str


register_resource(Gadget)
