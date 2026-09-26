"""Message-board MongoDB example app entry point.

The ``v2`` app is assembled from three pieces, mirroring the SQL example (01):

* a :class:`~resourcey.v2.mongo.mongo_client.MongoClientManager` (the clients,
  built from ``APP_MONGO_CONNECTIONS_*``);
* a :class:`~resourcey.v2.core.manifest.Manifest` (the resource set and its
  lifecycle);
* the :func:`~resourcey.v2.http.app.create_app` free function (which builds a
  runnable FastAPI app wired to the manifest's lifespan, routes, error handlers,
  and CORS).

Run with::

    uvicorn message_board.app:app --env-file .env --port 8082

Note the ``--env-file``: ``v2`` does no ``.env`` loading of its own, so the
process environment must be populated by the caller (uvicorn, or a shell).

The same manager instance is threaded into every resource **and** listed in the
manifest's ``managers`` slot. Both halves matter: the manifest enters the
manager's lifecycle (``async with manifest``), and the resources resolve their
collections from that same object. A resource left on the default
``get_mongo_client_manager()`` would use a *different*, un-entered manager and
fail at the first request.
"""

from __future__ import annotations

from message_board.message import MessageDTO, MessageResource
from message_board.thread import ThreadDTO, ThreadResource
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.http.dependency_builder import DependencyBuilder
from resourcey.v2.mongo.mongo_client import MongoClientManager
from resourcey.v2.mongo.mongo_config import MongoConfig

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_client_manager = MongoClientManager(MongoConfig.get_instance())


def build_app(
    dependency_builder: DependencyBuilder | None = None,
    client_manager: MongoClientManager | None = None,
):
    """Build the manifest + FastAPI app.

    Kept as a factory (rather than wiring only at import time) so the tests can
    build a fresh app, inject their own ``client_manager`` (e.g. one pointing at
    an isolated database), and supply a ``dependency_builder`` (the auth /
    authorization seam) without touching the resources. ``client_manager``
    defaults to ``default_client_manager``.
    """
    manager = client_manager or default_client_manager
    manifest = Manifest(
        resources=[
            ThreadResource(ThreadDTO, client_manager=manager),
            MessageResource(MessageDTO, client_manager=manager),
        ],
        managers=[manager],
    )
    return manifest, create_app(manifest, dependency_builder=dependency_builder)


manifest, app = build_app()
