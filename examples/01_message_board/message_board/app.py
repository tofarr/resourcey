"""Message-board example app entry point.

The ``v2`` app is assembled from three pieces: a
:class:`~resourcey.v2.sql.session_manager.SqlSessionManager` (the engines, built
from ``APP_SQL_CONNECTIONS_*``), a
:class:`~resourcey.v2.core.manifest.Manifest` (the resource set and its
lifecycle), and the :func:`~resourcey.v2.http.app.create_app` free function
(which builds a runnable FastAPI app wired to the manifest's lifespan, routes,
error handlers, and CORS). Run with::

    uvicorn message_board.app:app --env-file .env

Note the ``--env-file``: ``v2`` does no ``.env`` loading of its own, so the
process environment must be populated by the caller (uvicorn, or a shell).

The same manager instance is threaded into every resource **and** listed in the
manifest's ``managers`` slot. Both halves matter: the manifest enters the
manager's lifecycle (``async with manifest``), and the resources resolve their
sessions from that same object. A resource left on the default
``get_sql_session_manager()`` would use a *different*, un-entered manager and
fail at the first request.
"""

from __future__ import annotations

from message_board.message import MessageResource
from message_board.models import Message, Thread
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.http.dependency_builder import DependencyBuilder
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig
from resourcey.v2.sql.sql_resource import SqlResource

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_app(
    dependency_builder: DependencyBuilder | None = None,
    session_manager: SqlSessionManager | None = None,
):
    """Build the manifest + FastAPI app.

    Kept as a factory (rather than wiring only at import time) so the tests can
    build a fresh app, inject their own ``session_manager`` (e.g. one pointing at
    an isolated database), and supply a ``dependency_builder`` (the auth /
    authorization seam) without touching the resources. ``session_manager``
    defaults to ``default_session_manager``.
    """
    manager = session_manager or default_session_manager
    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_manager=manager),
            MessageResource(Message, session_manager=manager),
        ],
        managers=[manager],
    )
    return manifest, create_app(manifest, dependency_builder=dependency_builder)


manifest, app = build_app()
