"""Tests for the MongoDB message-board example.

Exercises the full CRUD lifecycle (create → read → update → delete) for
both Thread and Message via the app's TestClient, against the embedded
mongomock backend. No external MongoDB server is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from message_board.app import create_app
from message_board.message import Message
from message_board.thread import Thread


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Register + configure resources against a fresh embedded client.
    from resourcey.mongo.embedded import AsyncEmbeddedClient

    client_obj = AsyncEmbeddedClient()
    Thread.configure(client=client_obj, database_name="test")
    Message.configure(client=client_obj, database_name="test")

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestThreadLifecycle:
    @pytest.mark.asyncio
    async def test_create_thread(self, client: AsyncClient) -> None:
        resp = await client.post("/threads", json={"title": "Hello", "description": "world"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["description"] == "world"
        assert "id" in body

    @pytest.mark.asyncio
    async def test_read_thread(self, client: AsyncClient) -> None:
        create = await client.post("/threads", json={"title": "T"})
        tid = create.json()["id"]
        resp = await client.get(f"/threads/{tid}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "T"

    @pytest.mark.asyncio
    async def test_update_thread(self, client: AsyncClient) -> None:
        create = await client.post("/threads", json={"title": "Old"})
        tid = create.json()["id"]
        resp = await client.patch(f"/threads/{tid}", json={"title": "New"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New"

    @pytest.mark.asyncio
    async def test_delete_thread(self, client: AsyncClient) -> None:
        create = await client.post("/threads", json={"title": "Bye"})
        tid = create.json()["id"]
        resp = await client.delete(f"/threads/{tid}")
        assert resp.status_code == 204
        resp2 = await client.get(f"/threads/{tid}")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_search_threads(self, client: AsyncClient) -> None:
        for i in range(3):
            await client.post("/threads", json={"title": f"T{i}"})
        resp = await client.get("/threads")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 3


class TestMessageLifecycle:
    @pytest.mark.asyncio
    async def test_create_message(self, client: AsyncClient) -> None:
        t = await client.post("/threads", json={"title": "Thread"})
        tid = t.json()["id"]
        resp = await client.post("/messages", json={"thread_id": tid, "text": "Hi"})
        assert resp.status_code == 201
        assert resp.json()["text"] == "Hi"

    @pytest.mark.asyncio
    async def test_list_thread_messages(self, client: AsyncClient) -> None:
        t = await client.post("/threads", json={"title": "Thread"})
        tid = t.json()["id"]
        for i in range(3):
            await client.post("/messages", json={"thread_id": tid, "text": f"m{i}"})
        resp = await client.get("/messages", params={"thread_id__eq": tid})
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3


# Required for pytest-asyncio fixtures.
import pytest_asyncio
