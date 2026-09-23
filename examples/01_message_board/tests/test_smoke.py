"""Smoke test for the message-board example app.

Exercises the full HTTP path (create thread → create message → search filter)
against an in-memory SQLite database via httpx's ASGI transport. This guards
against framework regressions that would break the example.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from resourcey.manifest import ResourceManifest
from resourcey.resource.sql import ResourceyBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from message_board.message import Message
from message_board.thread import Thread


@pytest_asyncio.fixture
async def app() -> FastAPI:
    """Assemble the message-board app with an in-memory SQLite database."""
    from resourcey.app_context import AppContext
    from resourcey.config.config_framework import FrameworkConfig
    from resourcey.resource.sql import _SESSION_FACTORY_KEY

    manifest = ResourceManifest(resources=(Thread(), Message()))
    manifest.materialize()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    ctx = AppContext(FrameworkConfig())
    ctx.set(_SESSION_FACTORY_KEY, factory)
    app = manifest.create_app(app_context=ctx)
    # ASGITransport does not run the lifespan; enter the manifest manually
    # so each instance's __aenter__ copies the pre-seeded factory.
    await manifest.__aenter__()
    yield app
    await manifest.__aexit__(None, None, None)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_thread_and_message(client: AsyncClient):
    """POST a thread, POST a message under it, then GET both back."""
    # Create a thread
    resp = await client.post("/threads", json={"title": "Smoke test", "description": "hi"})
    assert resp.status_code == 201
    thread = resp.json()
    assert thread["title"] == "Smoke test"
    thread_id = thread["id"]

    # Create a message in that thread
    resp = await client.post("/messages", json={"thread_id": thread_id, "text": "hello world"})
    assert resp.status_code == 201
    message = resp.json()
    assert message["text"] == "hello world"
    assert message["thread_id"] == thread_id

    # Fetch the thread by id
    resp = await client.get(f"/threads/{thread_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Smoke test"


async def test_search_filter_thread_id(client: AsyncClient):
    """thread_id__eq filter returns only messages in the target thread."""
    # Seed two threads with messages
    t1 = (await client.post("/threads", json={"title": "T1"})).json()
    t2 = (await client.post("/threads", json={"title": "T2"})).json()
    await client.post("/messages", json={"thread_id": t1["id"], "text": "in T1"})
    await client.post("/messages", json={"thread_id": t2["id"], "text": "in T2"})

    # Filter messages by thread_id__eq
    resp = await client.get(f"/messages?thread_id__eq={t1['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "in T1"


async def test_search_filter_text_contains(client: AsyncClient):
    """text__contains filter does a substring search on message text."""
    t = (await client.post("/threads", json={"title": "T"})).json()
    await client.post("/messages", json={"thread_id": t["id"], "text": "hello world"})
    await client.post("/messages", json={"thread_id": t["id"], "text": "goodbye"})

    resp = await client.get("/messages?text__contains=hello")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["text"] == "hello world"
