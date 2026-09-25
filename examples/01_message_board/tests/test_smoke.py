"""Smoke test for the v2 message-board example app.

Exercises the full HTTP path (create thread → create message → filter) against
an in-memory SQLite database via httpx's ASGI transport. Storage is injected
through the ``session_factory=`` escape hatch (a shared in-memory engine), which
keeps this suite fast and independent of the committed migration — the migration
itself is verified in ``test_e2e.py``.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from message_board.message import MessageResource
from message_board.models import Base, Message, Thread
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.sql_resource import SqlResource


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """A v2 app over a shared in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_factory=maker),
            MessageResource(Message, session_factory=maker),
        ]
    )
    app: FastAPI = create_app(manifest)
    # ASGITransport does not run the lifespan; enter the manifest manually so the
    # resources' runtime lifecycle is active for the requests below.
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)
        await engine.dispose()


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
