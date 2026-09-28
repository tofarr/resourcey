"""File-store example app entry point.

This example shows the file store: file **bytes** live in a pluggable medium,
the client transfers them directly against a short-lived capability URL, and the
API owns only the **metadata** and the **authorization**. On the happy path the
bytes never pass through a request handler:

1. ``POST /files`` creates a metadata row (``pending``) and assigns an opaque key.
2. ``POST /files/{id}/upload-url`` mints a ``put`` capability URL.
3. The client ``PUT`` s the bytes directly against that URL.
4. ``POST /files/{id}/complete`` heads the object, verifies it, and flips the row
   to ``ready``.
5. ``GET /files/{id}/download`` mints a ``get`` capability for a ``ready`` file.

The medium decides *what the URL is*: :class:`~resourcey.filestore.s3_file_store.S3FileStore`
returns S3's own native SigV4 pre-signed URL, while the local / SQL media return
a framework-signed capability served by this app's own ``/_files/{key}``
``PUT`` / ``GET`` transfer endpoints. Because the S3 URL points straight at S3,
an S3 store mounts no transfer endpoint at all.

The app is assembled from the pieces the other examples use — a
:class:`~resourcey.sql.session_manager.SqlSessionManager` (the engines), a
:class:`~resourcey.core.manifest.Manifest` (the resource set and its lifecycle),
and :func:`~resourcey.http.app.create_app` — plus two file-store specifics:

* the **medium** (a :class:`~resourcey.filestore.file_store.FileStore`) is a
  config-selected object entered through the manifest's ``managers`` slot, so its
  client / directory lifecycle is tied to the app exactly as the session manager
  is;
* :func:`~resourcey.filestore.file_routes.register_file_routes` mounts the
  handshake routes **after** ``create_app`` — a presign handshake is genuinely
  not one of the eight standard resource actions, so it is not part of the
  manifest.

The medium is chosen with no code change: ``MEDIUM_CLASS`` names a ``FileStore``
subclass (default :class:`~resourcey.filestore.local_file_store.LocalFileStore`),
and its own fields parse under the ``MEDIUM_`` prefix (e.g. ``MEDIUM_ROOT``).

Run with::

    uvicorn file_store_example.app:app --env-file .env --port 8086

Note the ``--env-file``: the framework does no ``.env`` loading of its own, so
the process environment must be populated by the caller (uvicorn, or a shell).
The same manager / store instances are threaded into the resources **and** listed
in the manifest's ``managers`` slot; a resource left on the process-wide default
would use a different, un-entered object and fail at the first request.
"""

from __future__ import annotations

from fastapi import FastAPI

from file_store_example.files import build_files_resource
from resourcey.core.manifest import Manifest
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_routes import register_file_routes
from resourcey.filestore.file_store import FileStore
from resourcey.http.app import create_app
from resourcey.http.dependency_builder import DependencyBuilder
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig

# One manager / medium for the whole app; both are entered by the manifest.
# ``FileStoreConfig.medium`` is a lazy, env-selected FileStore instance
# (``MEDIUM_CLASS``), defaulting to LocalFileStore.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())
default_store: FileStore = FileStoreConfig.get_instance().medium


def build_app(
    *,
    dependency_builder: DependencyBuilder | None = None,
    session_manager: SqlSessionManager | None = None,
    store: FileStore | None = None,
    config: FileStoreConfig | None = None,
) -> tuple[Manifest, FastAPI]:
    """Build the manifest + FastAPI app, mounting the file handshake routes.

    Kept as a factory so the tests can inject an isolated ``session_manager`` /
    ``store`` (and a fresh ``config``) without touching the declarations.
    ``session_manager`` / ``store`` default to the module-level singletons, and
    ``config`` to :meth:`FileStoreConfig.get_instance`.
    """
    manager = session_manager or default_session_manager
    medium = store if store is not None else default_store
    resolved_config = config if config is not None else FileStoreConfig.get_instance()

    files = build_files_resource(medium, session_manager=manager)
    manifest = Manifest(resources=[files], managers=[manager, medium])
    app = create_app(manifest, dependency_builder=dependency_builder)
    register_file_routes(
        app, medium, resource=files, dependency_builder=dependency_builder, config=resolved_config
    )
    return manifest, app


manifest, app = build_app()
