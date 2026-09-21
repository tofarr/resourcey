"""Module-level manifest for migration tests (issue #51).

``env.py`` resolves ``RESOURCEY_MANIFEST`` (a ``module:attr`` path) and
materialises the manifest so its tables land in ``ResourceyBase.metadata``
before Alembic diffs. The manifest and its resource types must live at module
scope to be importable by dotted path.
"""

from __future__ import annotations

from migrate_resources_a import Widget

from resourcey.manifest import ResourceManifest

manifest = ResourceManifest(resources=(Widget,))
