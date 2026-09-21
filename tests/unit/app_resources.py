"""Module-level resource classes + manifest for ``ResourceManifest`` tests.

The manifest's dotted-path resolution (``RESOURCEY_MANIFEST``) imports a
module-level attribute, so the manifest and its resource types must live at
module scope (not local to a test function) to be importable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from resourcey.manifest import ResourceManifest
from resourcey.resource.sql import SqlResource


class AppWidget(SqlResource):
    """A simple resource for manifest integration tests — int id + label."""

    id: int
    label: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppGadget(SqlResource):
    """A second resource so multi-resource registration is exercised."""

    id: int
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Module-level manifest for migrate tests (``RESOURCEY_MANIFEST`` points here).
# Construction calls ``on_register`` on each resource, materialising tables.
manifest = ResourceManifest(resources=(AppWidget, AppGadget))
