"""File-store example app entry point.

This example shows the file store (issue #158): file **bytes** live in a
pluggable medium, the client transfers them directly against a short-lived
capability URL, and the API owns only **authorization**. There is no metadata
table -- "does the medium have the bytes" is the only source of truth for a
file's existence:

1. ``POST /files`` allocates an opaque key and mints an upload capability
   (``202``; nothing is persisted yet).
2. The client transfers the bytes directly against that capability (a ``PUT``
   for the local / SQL media, a presigned ``POST`` for S3).
3. The file now exists: ``GET /files/{id}`` / ``GET /files`` resolve directly
   against the medium.
4. ``GET /files/{id}/download`` mints a fresh ``get`` capability (the JSON
   shape -- for a non-browser client); ``GET /files/{id}/content`` fetches the
   bytes themselves (a redirect for S3, streamed directly for Local / SQL --
   see :mod:`resourcey.filestore.file_routes` for why both exist: a plain
   ``<a href>`` / ``<img src>`` cannot carry an API key).

The medium decides *what the upload/download URL is*:
:class:`~resourcey.filestore.s3_file_store.S3FileStore` returns S3's own native
SigV4 URLs, while the local / SQL media return a framework-signed capability
served by this app's own ``/_files/{key}`` ``PUT`` / ``GET`` transfer
endpoints.

The app is assembled from the pieces the other examples use — a
:class:`~resourcey.core.manifest.Manifest` (the resource set and its
lifecycle) and :func:`~resourcey.http.app.create_app` — plus two
file-store specifics:

* the **medium** (a :class:`~resourcey.filestore.file_store.FileStore`) is a
  config-selected object entered through the manifest's ``managers`` slot, so
  its client / directory lifecycle is tied to the app. The default
  :class:`~resourcey.filestore.local_file_store.LocalFileStore` needs no
  database at all; a :class:`~resourcey.sql.session_manager.SqlSessionManager`
  is only built and entered when ``.env`` selects the SQL medium
  (``MEDIUM_CLASS=...SqlFileStore``).
* :func:`~resourcey.filestore.file_routes.register_file_routes` mounts the
  whole ``files`` surface **after** ``create_app`` and is the **sole** place
  its routes are mounted — ``files`` is therefore *not* listed in
  ``Manifest(resources=...)`` (see that function's docstring for why: minting
  a ``202`` on create requires swapping out the generically generated ``201``
  route, which only works if ``register_routes`` is called exactly once, by
  ``register_file_routes`` itself).

The medium is chosen with no code change: ``MEDIUM_CLASS`` names a ``FileStore``
subclass (default :class:`~resourcey.filestore.local_file_store.LocalFileStore`),
and its own fields parse under the ``MEDIUM_`` prefix (e.g. ``MEDIUM_ROOT``).

Run with::

    uvicorn file_store_example.app:app --env-file .env --port 8086

Note the ``--env-file``: the framework does no ``.env`` loading of its own, so
the process environment must be populated by the caller (uvicorn, or a shell).
"""

from __future__ import annotations

from fastapi import FastAPI

from file_store_example.files import build_files_resource
from resourcey.core.manifest import Manifest
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_routes import register_file_routes
from resourcey.filestore.file_store import FileStore
from resourcey.filestore.sql_file_store import SqlFileStore
from resourcey.http.app import create_app
from resourcey.http.dependency_builder import DependencyBuilder
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig

# ``FileStoreConfig.medium`` is a lazy, env-selected FileStore instance
# (``MEDIUM_CLASS``), defaulting to LocalFileStore -- which needs no database.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())
default_store: FileStore = FileStoreConfig.get_instance().medium


def _resolve_store(store: FileStore, manager: SqlSessionManager) -> FileStore:
    """Wire the app's own session manager into a config-selected SQL medium.

    ``FileStoreConfig.medium``'s ``LazyField`` only parses the medium's typed
    fields from the environment (e.g. ``MEDIUM_CONNECTION_NAME``); it has no
    way to hand it a *live* manager instance, so a store built that way always
    falls back to the process-wide default at entry. Rebuilding it here with
    the app's own manager keeps its lifecycle tied to this app's manifest,
    exactly like every other resource.
    """
    if isinstance(store, SqlFileStore) and store.session_factory is None:
        return SqlFileStore(session_manager=manager, connection_name=store.connection_name)
    return store


def build_app(
    *,
    dependency_builder: DependencyBuilder | None = None,
    session_manager: SqlSessionManager | None = None,
    store: FileStore | None = None,
    config: FileStoreConfig | None = None,
) -> tuple[Manifest, FastAPI]:
    """Build the manifest + FastAPI app, mounting the ``files`` surface.

    Kept as a factory so the tests can inject an isolated ``session_manager`` /
    ``store`` (and a fresh ``config``) without touching the declarations.
    ``session_manager`` / ``store`` default to the module-level singletons, and
    ``config`` to :meth:`FileStoreConfig.get_instance`.
    """
    manager = session_manager or default_session_manager
    medium = _resolve_store(store if store is not None else default_store, manager)
    resolved_config = config if config is not None else FileStoreConfig.get_instance()

    files = build_files_resource(medium, max_size=resolved_config.max_size)
    # The SQL session manager is only needed (and only entered) when the
    # selected medium actually uses it.
    managers = [medium] if not isinstance(medium, SqlFileStore) else [manager, medium]
    # ``files`` is deliberately not a manifest resource -- see
    # register_file_routes's docstring.
    manifest = Manifest(resources=[], managers=managers)
    app = create_app(manifest, dependency_builder=dependency_builder)
    register_file_routes(
        app, medium, resource=files, dependency_builder=dependency_builder, config=resolved_config
    )
    return manifest, app


manifest, app = build_app()
