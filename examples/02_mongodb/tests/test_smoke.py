"""Smoke test for the v2 MongoDB message-board example app.

Exercises the full HTTP path (create thread → create message → filter) against
the embedded ``mongomock`` client via httpx's ASGI transport. Storage is injected
through the ``client_manager=`` seam pointed at an isolated embedded database,
which keeps this suite fast and independent of any external server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from message_board.app import build_app
from resourcey.v2.mongo.mongo_client import MongoClientManager
from resourcey.v2.mongo.mongo_config import MongoConfig, MongoConnectionConfig


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A v2 app over an isolated embedded Mongo database."""
    database = f"smoke_{uuid.uuid4().hex}"
    manager = MongoClientManager(
        MongoConfig(
            mongo_connections=[MongoConnectionConfig(name="main", url=f"embedded://{database}")]
        )
    )
    manifest, app = build_app(client_manager=manager)
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


async def test_create_thread_and_message(client: AsyncClient):
    """POST a thread, POST a message under it, then GET both back."""
    resp = await client.post("/threads", json={"title": "Smoke test", "description": "hi"})
    assert resp.status_code == 201
    thread = resp.json()
    assert thread["title"] == "Smoke test"
    assert thread["description"] == "hi"
    thread_id = thread["id"]

    resp = await client.post("/messages", json={"thread_id": thread_id, "text": "hello world"})
    assert resp.status_code == 201
    message = resp.json()
    assert message["text"] == "hello world"
    assert message["thread_id"] == thread_id

    resp = await client.get(f"/threads/{thread_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Smoke test"


async def test_search_filter_thread_id(client: AsyncClient):
    """thread_id__eq returns only messages in the target thread."""
    t1 = (await client.post("/threads", json={"title": "T1"})).json()
    t2 = (await client.post("/threads", json={"title": "T2"})).json()
    await client.post("/messages", json={"thread_id": t1["id"], "text": "in T1"})
    await client.post("/messages", json={"thread_id": t2["id"], "text": "in T2"})

    resp = await client.get(f"/messages?thread_id__eq={t1['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "in T1"


async def test_search_filter_text_contains(client: AsyncClient):
    """text__contains does a substring search on message text."""
    t = (await client.post("/threads", json={"title": "T"})).json()
    await client.post("/messages", json={"thread_id": t["id"], "text": "hello world"})
    await client.post("/messages", json={"thread_id": t["id"], "text": "goodbye"})

    resp = await client.get("/messages?text__contains=hello")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "hello world"


async def test_unknown_filter_param_is_rejected(client: AsyncClient):
    """A ``field__op`` outside the declared surface is a 400, not silently ignored."""
    resp = await client.get("/messages?bogus__eq=1")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
