"""End-to-end REST API tests for the v2 message-board example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration itself is
verified, not bypassed with ``create_all``.

The app is assembled through the real config path: the isolated URL is set as
``APP_SQL_CONNECTIONS_0_URL`` and a fresh
:class:`~resourcey.v2.sql.session_manager.SqlSessionManager` is built from
``SqlConfig.get_instance()``, then handed to :func:`message_board.app.build_app`.
Requests run through httpx's ASGI transport — the full request → router →
service → SQLAlchemy stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient

from message_board.app import build_app
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _sync_url(async_url: str) -> str:
    """Alembic drives a sync engine; mirror ``migrations/env.py``'s conversion."""
    return async_url.replace("+aiosqlite", "")


def _apply_migration(async_url: str) -> None:
    """Apply the committed migration to the isolated database."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(_migrations_dir()))
    # env.py prefers an explicit sqlalchemy.url, so no APP_* env is needed here.
    config.set_main_option("sqlalchemy.url", _sync_url(async_url))
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A fully wired v2 REST client backed by an isolated, migrated SQLite file."""
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager)
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


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
        assert body["id"] == 1
        assert "created_at" in body
        assert "updated_at" in body

    async def test_read_returns_thread_by_id(self, client: AsyncClient) -> None:
        created = (await client.post("/threads", json={"title": "Read me"})).json()
        resp = await client.get(f"/threads/{created['id']}")
        assert resp.status_code == 200
        assert resp.json() == created

    async def test_read_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/threads/999")
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
        # description is preserved (PATCH = partial merge, not replace)
        assert body["description"] == "keep"

    async def test_delete_then_read_404(self, client: AsyncClient) -> None:
        created = (await client.post("/threads", json={"title": "Bye"})).json()
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


async def _make_thread(client: AsyncClient, title: str = "T") -> dict[str, object]:
    return (await client.post("/threads", json={"title": title})).json()


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
        assert resp.json() == created

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

    async def test_declared_filter_surface_rejects_other_fields(self, client: AsyncClient) -> None:
        """The declared filter class is the whole surface: ``id__eq`` is rejected."""
        await _make_thread(client)
        resp = await client.get("/messages?id__eq=1")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_input"


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
        assert [t["id"] for t in body] == ids

    async def test_batch_read_missing_id_returns_null(self, client: AsyncClient) -> None:
        created = (await client.post("/threads", json={"title": "Real"})).json()
        resp = await client.get("/threads/batch-read", params=[("id", created["id"]), ("id", 9999)])
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["id"] == created["id"]
        assert body[1] is None
