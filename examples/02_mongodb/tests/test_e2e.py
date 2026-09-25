"""End-to-end REST API tests for the v2 MongoDB message-board example.

Each test runs against an **isolated embedded MongoDB database** — a unique
database name per test on the in-process mongomock server (no external MongoDB
required, no shared state between tests). MongoDB resources have no Alembic
migrations; the schema is created implicitly on first write, so the suite
verifies the full request → router → service → motor/mongomock stack without any
migration step.

The app is assembled with a fresh ``MongoClientManager`` pointed at the isolated
database, entered via the manifest's async lifecycle (so the client is built
through the real config path), and exercised through httpx's ASGI transport.
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
    """A fully wired REST client backed by an isolated embedded Mongo database."""
    database = f"e2e_{uuid.uuid4().hex}"
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


async def _make_thread(client: AsyncClient, title: str = "T") -> dict[str, object]:
    return (await client.post("/threads", json={"title": title})).json()


# ---------------------------------------------------------------------------
# Thread CRUD
# ---------------------------------------------------------------------------


class TestThreadCrud:
    async def test_create_returns_created_thread(self, client: AsyncClient) -> None:
        resp = await client.post("/threads", json={"title": "Hello", "description": "world"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["description"] == "world"
        assert "id" in body
        assert "created_at" in body

    async def test_read_returns_thread_by_id(self, client: AsyncClient) -> None:
        created = await _make_thread(client, "Read me")
        resp = await client.get(f"/threads/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Read me"

    async def test_read_missing_returns_404(self, client: AsyncClient) -> None:
        missing_id = str(uuid.uuid4())
        resp = await client.get(f"/threads/{missing_id}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_partial_merge(self, client: AsyncClient) -> None:
        created = (
            await client.post("/threads", json={"title": "Old", "description": "keep"})
        ).json()
        resp = await client.patch(f"/threads/{created['id']}", json={"title": "New"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New"
        assert body["description"] == "keep"

    async def test_delete_then_read_404(self, client: AsyncClient) -> None:
        created = await _make_thread(client, "Bye")
        resp = await client.delete(f"/threads/{created['id']}")
        assert resp.status_code == 204
        resp = await client.get(f"/threads/{created['id']}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Thread search, sort, count, pagination
# ---------------------------------------------------------------------------


class TestThreadSearch:
    async def test_search_returns_all_paginated(self, client: AsyncClient) -> None:
        for i in range(3):
            await client.post("/threads", json={"title": f"T{i}"})
        resp = await client.get("/threads")
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 20
        assert body["next_cursor"] is None
        assert len(body["items"]) == 3

    async def test_count_returns_row_count(self, client: AsyncClient) -> None:
        for i in range(4):
            await client.post("/threads", json={"title": f"T{i}"})
        resp = await client.get("/threads/count")
        assert resp.status_code == 200
        assert resp.json() == 4

    async def test_search_sort_ascending(self, client: AsyncClient) -> None:
        for title in ("Cherry", "Apple", "Banana"):
            await client.post("/threads", json={"title": title})
        resp = await client.get("/threads?sort=title")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()["items"]]
        assert titles == ["Apple", "Banana", "Cherry"]

    async def test_search_sort_descending(self, client: AsyncClient) -> None:
        for title in ("Cherry", "Apple", "Banana"):
            await client.post("/threads", json={"title": title})
        resp = await client.get("/threads?sort=title&desc=true")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()["items"]]
        assert titles == ["Cherry", "Banana", "Apple"]

    async def test_search_cursor_pagination(self, client: AsyncClient) -> None:
        for i in range(5):
            await client.post("/threads", json={"title": f"T{i:02d}"})
        page1 = (await client.get("/threads?limit=2&sort=title")).json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"] is not None
        assert [t["title"] for t in page1["items"]] == ["T00", "T01"]

        page2 = (
            await client.get(f"/threads?limit=2&sort=title&cursor={page1['next_cursor']}")
        ).json()
        assert len(page2["items"]) == 2
        assert [t["title"] for t in page2["items"]] == ["T02", "T03"]

        page3 = (
            await client.get(f"/threads?limit=2&sort=title&cursor={page2['next_cursor']}")
        ).json()
        assert len(page3["items"]) == 1
        assert page3["next_cursor"] is None
        assert page3["items"][0]["title"] == "T04"


# ---------------------------------------------------------------------------
# Message CRUD + thread-scoped filtering
# ---------------------------------------------------------------------------


class TestMessageCrud:
    async def test_create_message_under_thread(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        resp = await client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["text"] == "hi"
        assert body["thread_id"] == thread["id"]

    async def test_read_message(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        created = (
            await client.post("/messages", json={"thread_id": thread["id"], "text": "hello"})
        ).json()
        resp = await client.get(f"/messages/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello"

    async def test_update_message_text(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        created = (
            await client.post("/messages", json={"thread_id": thread["id"], "text": "old"})
        ).json()
        resp = await client.patch(f"/messages/{created['id']}", json={"text": "new"})
        assert resp.status_code == 200
        assert resp.json()["text"] == "new"

    async def test_delete_message(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        created = (
            await client.post("/messages", json={"thread_id": thread["id"], "text": "bye"})
        ).json()
        resp = await client.delete(f"/messages/{created['id']}")
        assert resp.status_code == 204
        resp = await client.get(f"/messages/{created['id']}")
        assert resp.status_code == 404


class TestMessageSearch:
    async def test_filter_by_thread_id(self, client: AsyncClient) -> None:
        t1 = await _make_thread(client, "T1")
        t2 = await _make_thread(client, "T2")
        for i in range(3):
            await client.post("/messages", json={"thread_id": t1["id"], "text": f"in T1 {i}"})
        await client.post("/messages", json={"thread_id": t2["id"], "text": "in T2"})

        resp = await client.get(f"/messages?thread_id__eq={t1['id']}")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        assert all(m["thread_id"] == t1["id"] for m in items)

    async def test_filter_text_contains(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        await client.post("/messages", json={"thread_id": thread["id"], "text": "hello world"})
        await client.post("/messages", json={"thread_id": thread["id"], "text": "goodbye"})

        resp = await client.get("/messages?text__contains=hello")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["text"] == "hello world"

    async def test_count_with_filter(self, client: AsyncClient) -> None:
        t1 = await _make_thread(client, "T1")
        t2 = await _make_thread(client, "T2")
        for _ in range(4):
            await client.post("/messages", json={"thread_id": t1["id"], "text": "x"})
        await client.post("/messages", json={"thread_id": t2["id"], "text": "y"})

        resp = await client.get(f"/messages/count?thread_id__eq={t1['id']}")
        assert resp.status_code == 200
        assert resp.json() == 4

    async def test_sort_messages_by_text(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        for text in ("delta", "alpha", "charlie", "bravo"):
            await client.post("/messages", json={"thread_id": thread["id"], "text": text})

        resp = await client.get(f"/messages?thread_id__eq={thread['id']}&sort=text")
        assert resp.status_code == 200
        texts = [m["text"] for m in resp.json()["items"]]
        assert texts == ["alpha", "bravo", "charlie", "delta"]

    async def test_message_cursor_pagination(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        for i in range(5):
            await client.post("/messages", json={"thread_id": thread["id"], "text": f"msg{i:02d}"})

        page1 = (
            await client.get(f"/messages?thread_id__eq={thread['id']}&limit=2&sort=text")
        ).json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"] is not None

        page2 = (
            await client.get(
                f"/messages?thread_id__eq={thread['id']}&limit=2&sort=text"
                f"&cursor={page1['next_cursor']}"
            )
        ).json()
        assert len(page2["items"]) == 2

        page3 = (
            await client.get(
                f"/messages?thread_id__eq={thread['id']}&limit=2&sort=text"
                f"&cursor={page2['next_cursor']}"
            )
        ).json()
        assert len(page3["items"]) == 1
        assert page3["next_cursor"] is None

        all_texts = (
            [m["text"] for m in page1["items"]]
            + [m["text"] for m in page2["items"]]
            + [m["text"] for m in page3["items"]]
        )
        assert all_texts == [f"msg{i:02d}" for i in range(5)]


# ---------------------------------------------------------------------------
# Batch read
# ---------------------------------------------------------------------------


class TestBatchRead:
    async def test_batch_read_threads(self, client: AsyncClient) -> None:
        ids = []
        for i in range(3):
            created = (await client.post("/threads", json={"title": f"T{i}"})).json()
            ids.append(created["id"])

        resp = await client.get("/threads/batch-read", params=[("id", i) for i in ids])
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        assert {t["id"] for t in body} == set(ids)

    async def test_batch_read_missing_id_returns_null(self, client: AsyncClient) -> None:
        created = (await client.post("/threads", json={"title": "Real"})).json()
        missing_id = str(uuid.uuid4())
        resp = await client.get(
            "/threads/batch-read", params=[("id", created["id"]), ("id", missing_id)]
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["id"] == created["id"]
        assert body[1] is None
