"""The ``files`` resource — the metadata half of the file store.

The byte half is a :class:`~resourcey.filestore.file_store.FileStore`; the
metadata half is an ordinary SQL resource, so it gets the full standard surface
(create / read / update / delete / search / count / batch) plus cache headers.
:func:`build_files_resource` wires the two together through
:func:`~resourcey.filestore.file_metadata.file_resource`, which:

* assigns the opaque storage ``key`` and the ``pending`` status on create
  (server-owned, never client-supplied);
* removes the stored object when a row is deleted, so no orphan remains;
* validates an optional declared-size cap (``APP_MAX_SIZE``);
* caches reads with a strong ETag over the projected bytes.

A client addresses a file by ``id``; the ``key`` is hidden from every response
and reachable only through the handshake routes.
"""

from __future__ import annotations

from resourcey.filestore.file_metadata import FileMetadataResource, file_resource
from resourcey.filestore.file_store import FileStore
from resourcey.sql.session_manager import SqlSessionManager


def build_files_resource(
    store: FileStore,
    *,
    session_manager: SqlSessionManager | None = None,
    path: str = "files",
) -> FileMetadataResource:
    """The conventional ``files`` metadata resource over ``store``.

    ``store`` is the same medium instance registered in the manifest's
    ``managers`` slot (so it is entered with the app); ``session_manager`` is the
    app's manager, threaded the same way the board's resources thread it.
    """
    return file_resource(store, path=path, session_manager=session_manager)
