"""Message-board MongoDB example app entry point.

With resources owning their own backend lifecycle (issue #49), the Mongo
example uses the framework's :func:`resourcey.app.create_app` — the same
factory as the SQL example. Each ``MongoResource`` builds its client from
``config.mongo`` (``RESOURCEY_MONGO_URL`` / ``RESOURCEY_MONGO_DATABASE``) in
its :meth:`~resourcey.mongo.mongo_resource.MongoResource.lifespan`, so the
factory stays storage-agnostic and ``resourcey run`` works for both
backends.

Run with::

    resourcey run

or directly with uvicorn::

    uvicorn message_board.app:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from resourcey.app import create_app as _create_framework_app

# Importing the resources module registers Thread + Message before the app
# factory reads the resource set.
import message_board.resources  # noqa: F401
from message_board.message import Message
from message_board.thread import Thread


def create_app() -> FastAPI:
    """Assemble the message-board FastAPI app (MongoDB via config)."""
    return _create_framework_app(resources=[Thread, Message])

