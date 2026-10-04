"""End-to-end REST + realtime tests for the realtime example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic
migration — the same revision ``alembic upgrade head`` applies — so the
migration itself is verified, not bypassed with ``create_all``.

The app is assembled through the real config path:
``realtime_example.app.build_app`` (the same entry point ``uvicorn``
targets), given an isolated session manager and a fresh ``InMemoryChannel()``
so the committed ``.env``'s entries are never needed for the suite to be
deterministic. ``background=False`` makes every trigger fire inline, so a
publish is always complete before the response is asserted on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from realtime_example.app import build_app
from resourcey.realtime.realtime_channel import InMemoryChannel
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _sync_url(async_url: str) -> str:
    """Alembic drives a sync engine; mirror ``migrations/env.py``'s conversion."""
    return async_url.replace("+aiosqlite", "")


def _apply_migration(async_url: str) -> None:
    """Apply the committed migration to the isolated database."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(_migrations_dir()))
    config.set_main_option("sqlalchemy.url", _sync_url(async_url))
    command.upgrade(config, "head")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A fully wired client backed by an isolated, migrated SQLite file."""
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    _manifest, app, _channel = build_app(
        session_manager=manager, channel=InMemoryChannel(), background=False
    )
    with TestClient(app) as c:
        yield c


def _make_thread(client: TestClient, title: str = "T") -> dict[str, object]:
    return client.post("/threads", json={"title": title}).json()


# ---------------------------------------------------------------------------
# The board itself still works end to end (realtime is a side effect)
# ---------------------------------------------------------------------------


class TestThreadCrud:
    def test_create_returns_created_thread(self, client: TestClient) -> None:
        resp = client.post("/threads", json={"title": "Hello", "description": "world"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["id"] == 1

    def test_read_missing_returns_404(self, client: TestClient) -> None:
        resp = client.get("/threads/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_update_partial_merge(self, client: TestClient) -> None:
        created = _make_thread(client, "Old")
        resp = client.patch(f"/threads/{created['id']}", json={"title": "New"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New"

    def test_delete_then_read_404(self, client: TestClient) -> None:
        created = _make_thread(client, "Bye")
        resp = client.delete(f"/threads/{created['id']}")
        assert resp.status_code == 204
        resp = client.get(f"/threads/{created['id']}")
        assert resp.status_code == 404


class TestMessageCrud:
    def test_create_message_under_thread(self, client: TestClient) -> None:
        thread = _make_thread(client)
        resp = client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
        assert resp.status_code == 201
        assert resp.json()["thread_id"] == thread["id"]

    def test_filter_by_thread_id(self, client: TestClient) -> None:
        t1 = _make_thread(client, "T1")
        t2 = _make_thread(client, "T2")
        for i in range(3):
            client.post("/messages", json={"thread_id": t1["id"], "text": f"in T1 {i}"})
        client.post("/messages", json={"thread_id": t2["id"], "text": "in T2"})

        resp = client.get(f"/messages?thread_id__eq={t1['id']}")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        assert all(m["thread_id"] == t1["id"] for m in items)


# ---------------------------------------------------------------------------
# The realtime channel, over the committed-migration-backed app
# ---------------------------------------------------------------------------


class TestRealtimeDelivery:
    def test_direct_wiring_fires_on_thread_create(self, client: TestClient) -> None:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "threads"})
            ws.receive_json()  # ack

            resp = client.post("/threads", json={"title": "Hello"})
            assert resp.status_code == 201

            event = ws.receive_json()
            assert event["event"]["resource"] == "threads"
            assert event["event"]["kind"] == "created"

    def test_messages_and_threads_are_independent_subscriptions(self, client: TestClient) -> None:
        thread = _make_thread(client)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "messages"})
            ws.receive_json()  # ack

            client.patch(f"/threads/{thread['id']}", json={"title": "Edited"})
            client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})

            event = ws.receive_json()
            assert event["event"]["resource"] == "messages"

    def test_batch_edit_publishes_one_event_per_item(self, client: TestClient) -> None:
        """A batch of three creates publishes three ``messages`` events."""
        thread = _make_thread(client)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "messages"})
            ws.receive_json()  # ack

            resp = client.post(
                "/messages/batch-edit",
                json=[
                    {"kind": "Create", "item": {"thread_id": thread["id"], "text": "first"}},
                    {"kind": "Create", "item": {"thread_id": thread["id"], "text": "second"}},
                    {"kind": "Create", "item": {"thread_id": thread["id"], "text": "third"}},
                ],
            )
            assert resp.status_code == 200

            texts = {ws.receive_json()["event"]["item"]["text"] for _ in range(3)}
            assert texts == {"first", "second", "third"}

    def test_asyncapi_docs_are_mounted(self, client: TestClient) -> None:
        resp = client.get("/asyncapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["asyncapi"] == "2.6.0"
        assert "/ws" in spec["channels"]
        assert "ThreadsEvent" in spec["components"]["schemas"]
        assert "MessagesEvent" in spec["components"]["schemas"]

        resp = client.get("/asyncapi")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
