"""Message-board MongoDB example app entry point.

Unlike the SQL example (which uses :func:`resourcey.app.create_app` to wire
SQLAlchemy engines and sessions), the MongoDB variant assembles the FastAPI
app directly: it creates a ``motor`` client (or an embedded ``mongomock``
client for local development), configures each Mongo resource, and mounts the
standard REST routes via :func:`resourcey.resource.routes.register_routes`.

This is the "escape hatch" principle in action: the framework does not hide
FastAPI, and a non-SQL backend can wire itself without going through the
SQL-centric app factory.

Run with::

    python -m message_board.app

or directly with uvicorn::

    uvicorn message_board.app:create_app --factory --reload
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from resourcey.resource.base import BaseResource
from resourcey.resource.routes import register_error_handlers, register_routes

# Importing the resources module registers Thread + Message.
import message_board.resources  # noqa: F401
from message_board.message import Message
from message_board.thread import Thread


def _create_client(database_name: str) -> Any:
    """Build a Mongo client from ``RESOURCEY_MONGO_URL``.

    When the URL is ``embedded`` (or empty), use the in-process
    ``mongomock``-backed async client — no external MongoDB server required.
    Otherwise, build a real ``motor`` client against the URL.
    """
    mongo_url = os.environ.get("RESOURCEY_MONGO_URL", "embedded").strip()
    if not mongo_url or mongo_url == "embedded":
        from resourcey.mongo.embedded import AsyncEmbeddedClient

        return AsyncEmbeddedClient()

    from motor.motor_asyncio import AsyncIOMotorClient

    return AsyncIOMotorClient(mongo_url)


def create_app(
    *,
    resources: Sequence[type[BaseResource]] | None = None,
) -> FastAPI:
    """Assemble the message-board FastAPI app (MongoDB via motor or embedded)."""
    database_name = os.environ.get("RESOURCEY_MONGO_DATABASE", "message_board")
    client = _create_client(database_name)

    resolved = list(resources) if resources is not None else [Thread, Message]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            for resource in resolved:
                await resource.ensure_indexes()  # type: ignore[attr-defined]
            yield
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    app = FastAPI(lifespan=lifespan)
    _configure_cors(app, ["*"])
    register_error_handlers(app)

    for resource in resolved:
        resource.configure(client=client, database_name=database_name)  # type: ignore[attr-defined]
        register_routes(app, resource)

    return app


def _configure_cors(app: FastAPI, cors_origins: list[str]) -> None:
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("RESOURCEY_HOST", "0.0.0.0")
    port = int(os.environ.get("RESOURCEY_PORT", "8082"))
    uvicorn.run("message_board.app:create_app", host=host, port=port, reload=True)
