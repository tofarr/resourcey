"""The ``files`` resource — a medium-native existence record (issue #158).

There is no metadata table: :class:`~resourcey.filestore.file_resource.FileResource`
serves ``files`` directly over whichever :class:`~resourcey.filestore.file_store.FileStore`
medium the app selects (``store``, the same instance registered in the
manifest's ``managers`` slot), so "does the medium have the bytes" is the only
source of truth for a file's existence. :func:`build_files_resource` is a thin
wrapper so ``app.py`` need not import :class:`FileResource` directly.
"""

from __future__ import annotations

from resourcey.filestore.file_resource import FileResource
from resourcey.filestore.file_store import FileStore


def build_files_resource(
    store: FileStore,
    *,
    path: str = "files",
    max_size: int | None = None,
) -> FileResource[object, str]:
    """The conventional ``files`` resource over ``store``."""
    return FileResource(store, path=path, max_size=max_size)
