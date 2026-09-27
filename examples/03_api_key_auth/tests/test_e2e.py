"""End-to-end REST API tests for the v2 API-key-auth example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration itself is
verified, not bypassed with ``create_all``.

The app is assembled through the real config path: the isolated URL is set as
``APP_SQL_CONNECTIONS_0_URL`` and a fresh
:class:`~resourcey.v2.sql.session_manager.SqlSessionManager` is built from
``SqlConfig.get_instance()``, then handed to
:func:`api_key_auth.app.build_app` together with the accepted keys. Requests run
through httpx's ASGI transport — the full request → auth → router → service →
SQLAlchemy stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from api_key_auth.app import build_app
from resourcey.v2.auth.auth_api_key import API_KEY_HEADER_NAME
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig

# The key the tests present; the same value ``.env`` sets.
_API_KEY = "example-api-key"


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


def _keys(*values: str) -> ApiKeysConfig:
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id=f"k{i}", name=None, key=SecretStr(value))
            for i, value in enumerate(values)
        ]
    )


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A fully wired, authenticated v2 REST client backed by a migrated SQLite file."""
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager, keys=_keys(_API_KEY))
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def authed(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """The client with the accepted key attached to every request."""
    client.headers[API_KEY_HEADER_NAME] = _API_KEY
    yield client


# ---------------------------------------------------------------------------
# The posture
# ---------------------------------------------------------------------------


class TestPosture:
    async def test_missing_key_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/threads")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")

    async def test_wrong_key_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/threads", headers={API_KEY_HEADER_NAME: "nope"})
        assert resp.status_code == 401

    async def test_valid_key_unlocks_crud(self, authed: AsyncClient) -> None:
        created = await authed.post("/threads", json={"title": "Hello", "description": "world"})
        assert created.status_code == 201
        body = created.json()
        assert body["title"] == "Hello"
        assert body["description"] == "world"
        assert body["id"] == 1
        assert "created_at" in body
        assert "updated_at" in body

    async def test_bearer_key_is_accepted(self, client: AsyncClient) -> None:
        resp = await client.get("/threads", headers={"Authorization": f"Bearer {_API_KEY}"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Thread CRUD
# ---------------------------------------------------------------------------


class TestThreadCrud:
    async def test_read_returns_thread_by_id(self, authed: AsyncClient) -> None:
        created = (await authed.post("/threads", json={"title": "Read me"})).json()
        resp = await authed.get(f"/threads/{created['id']}")
        assert resp.status_code == 200
        assert resp.json() == created

    async def test_read_missing_returns_404(self, authed: AsyncClient) -> None:
        resp = await authed.get("/threads/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_partial_merge(self, authed: AsyncClient) -> None:
        created = (
            await authed.post("/threads", json={"title": "Old", "description": "keep"})
        ).json()
        resp = await authed.patch(f"/threads/{created['id']}", json={"title": "New"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New"
        # description is preserved (PATCH = partial merge, not replace)
        assert body["description"] == "keep"

    async def test_delete_then_read_404(self, authed: AsyncClient) -> None:
        created = (await authed.post("/threads", json={"title": "Bye"})).json()
        resp = await authed.delete(f"/threads/{created['id']}")
        assert resp.status_code == 204
        resp = await authed.get(f"/threads/{created['id']}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Thread search, sort, count, pagination
# ---------------------------------------------------------------------------


class TestThreadSearch:
    async def test_search_returns_all_paginated(self, authed: AsyncClient) -> None:
        for i in range(3):
            await authed.post("/threads", json={"title": f"T{i}"})
        resp = await authed.get("/threads")
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 20
        assert body["next_cursor"] is None
        assert len(body["items"]) == 3

    async def test_count_returns_row_count(self, authed: AsyncClient) -> None:
        for i in range(4):
            await authed.post("/threads", json={"title": f"T{i}"})
        resp = await authed.get("/threads/count")
        assert resp.status_code == 200
        assert resp.json() == 4

    async def test_search_sort_descending(self, authed: AsyncClient) -> None:
        for title in ("Cherry", "Apple", "Banana"):
            await authed.post("/threads", json={"title": title})
        resp = await authed.get("/threads?sort=title&desc=true")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()["items"]]
        assert titles == ["Cherry", "Banana", "Apple"]

    async def test_search_cursor_pagination(self, authed: AsyncClient) -> None:
        for i in range(5):
            await authed.post("/threads", json={"title": f"T{i:02d}"})
        page1 = (await authed.get("/threads?limit=2&sort=title")).json()
        assert [t["title"] for t in page1["items"]] == ["T00", "T01"]
        assert page1["next_cursor"] is not None

        page2 = (
            await authed.get(f"/threads?limit=2&sort=title&cursor={page1['next_cursor']}")
        ).json()
        assert [t["title"] for t in page2["items"]] == ["T02", "T03"]

        page3 = (
            await authed.get(f"/threads?limit=2&sort=title&cursor={page2['next_cursor']}")
        ).json()
        assert [t["title"] for t in page3["items"]] == ["T04"]
        assert page3["next_cursor"] is None


# ---------------------------------------------------------------------------
# Message CRUD + thread-scoped filtering
# ---------------------------------------------------------------------------


async def _make_thread(client: AsyncClient, title: str = "T") -> dict[str, object]:
    return (await client.post("/threads", json={"title": title})).json()


class TestMessageCrud:
    async def test_create_message_under_thread(self, authed: AsyncClient) -> None:
        thread = await _make_thread(authed)
        resp = await authed.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["text"] == "hi"
        assert body["thread_id"] == thread["id"]

    async def test_update_message_text(self, authed: AsyncClient) -> None:
        thread = await _make_thread(authed)
        created = (
            await authed.post("/messages", json={"thread_id": thread["id"], "text": "old"})
        ).json()
        resp = await authed.patch(f"/messages/{created['id']}", json={"text": "new"})
        assert resp.status_code == 200
        assert resp.json()["text"] == "new"


class TestMessageSearch:
    async def test_filter_by_thread_id(self, authed: AsyncClient) -> None:
        t1 = await _make_thread(authed, "T1")
        t2 = await _make_thread(authed, "T2")
        for i in range(3):
            await authed.post("/messages", json={"thread_id": t1["id"], "text": f"in T1 {i}"})
        await authed.post("/messages", json={"thread_id": t2["id"], "text": "in T2"})

        resp = await authed.get(f"/messages?thread_id__eq={t1['id']}")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        assert all(m["thread_id"] == t1["id"] for m in items)

    async def test_filter_text_contains(self, authed: AsyncClient) -> None:
        thread = await _make_thread(authed)
        await authed.post("/messages", json={"thread_id": thread["id"], "text": "hello world"})
        await authed.post("/messages", json={"thread_id": thread["id"], "text": "goodbye"})

        resp = await authed.get("/messages?text__contains=hello")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["text"] == "hello world"


# ---------------------------------------------------------------------------
# The exposed key resource
# ---------------------------------------------------------------------------


class TestKeyResource:
    async def test_key_list_is_readable_but_hides_the_digest(self, authed: AsyncClient) -> None:
        resp = await authed.get("/api-keys")
        assert resp.status_code == 200
        assert resp.json()["items"] == [{"id": "k0", "name": None}]

    async def test_key_is_not_queryable(self, authed: AsyncClient) -> None:
        filtered = await authed.get("/api-keys?key__eq=x")
        sorted_ = await authed.get("/api-keys?sort=key")
        assert filtered.status_code == 400
        assert sorted_.status_code == 400

    async def test_key_resource_is_read_only(self, authed: AsyncClient) -> None:
        created = await authed.post("/api-keys", json={"name": "new"})
        assert created.status_code == 405
