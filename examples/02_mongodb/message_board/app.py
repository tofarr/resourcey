"""Message-board MongoDB example app entry point.

Declares a :class:`ResourceManifest` with the two resources and builds a
FastAPI app from it. Each ``MongoResource`` builds its client from
``config.mongo`` (``RESOURCEY_MONGO_URL`` / ``RESOURCEY_MONGO_DATABASE``) in
its ``__aenter__``, so the manifest stays storage-agnostic.

Run with::

    uvicorn message_board.app:app --reload
"""

from __future__ import annotations

from resourcey.manifest import ResourceManifest

from message_board.message import Message
from message_board.thread import Thread

manifest = ResourceManifest(resources=(Thread(), Message()))
app = manifest.create_app()

