"""Message-board example app entry point.

The app's :class:`~resourcey.manifest.ResourceManifest` owns the resource
instances and their lifecycle. ``manifest.create_app()`` builds a runnable
FastAPI app wired to the manifest's lifespan, routes, error handlers, and
CORS. Run with::

    uvicorn message_board.app:app

or::

    uvicorn message_board.app:app --reload
"""

from __future__ import annotations

from resourcey.manifest import ResourceManifest

from message_board.message import Message
from message_board.thread import Thread

manifest = ResourceManifest(resources=(Thread, Message))
app = manifest.create_app()
