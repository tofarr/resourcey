"""End-to-end REST API tests for the webhooks example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration itself is
verified, not bypassed with ``create_all``.

The app is assembled through the real config path:
``webhooks_example.app.build_app`` (the same entry point ``uvicorn`` targets),
given an isolated session manager and an explicit ``TriggerConfig`` so the
committed ``.env``'s entries are never needed for the suite to be
deterministic. ``background=False`` makes every trigger fire inline, so a log
line is always written before the response is asserted on.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient

from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.triggers.trigger_config import TriggerConfig, TriggerEntry
from webhooks_example.app import build_app
from webhooks_example.triggers import LoggingWebhookTrigger


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
    """A fully wired REST client backed by an isolated, migrated SQLite file.

    Configures a ``messages`` trigger exactly as the committed ``.env`` does
    (same resource path, same trigger kind), but built directly rather than
    through env parsing, to keep this fixture independent of ``.env``.
    """
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    trigger_config = TriggerConfig(
        triggers=[
            TriggerEntry(
                resource_path="messages",
                trigger=LoggingWebhookTrigger(name="slack-notify"),
            )
        ]
    )
    manifest, app, _builder = build_app(
        session_manager=manager, trigger_config=trigger_config, background=False
    )
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
# The board itself still works end to end (triggers are a side effect)
# ---------------------------------------------------------------------------


class TestThreadCrud:
    async def test_create_returns_created_thread(self, client: AsyncClient) -> None:
        resp = await client.post("/threads", json={"title": "Hello", "description": "world"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["id"] == 1

    async def test_read_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/threads/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_partial_merge(self, client: AsyncClient) -> None:
        created = await _make_thread(client, "Old")
        resp = await client.patch(f"/threads/{created['id']}", json={"title": "New"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New"

    async def test_delete_then_read_404(self, client: AsyncClient) -> None:
        created = await _make_thread(client, "Bye")
        resp = await client.delete(f"/threads/{created['id']}")
        assert resp.status_code == 204
        resp = await client.get(f"/threads/{created['id']}")
        assert resp.status_code == 404


class TestMessageCrud:
    async def test_create_message_under_thread(self, client: AsyncClient) -> None:
        thread = await _make_thread(client)
        resp = await client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
        assert resp.status_code == 201
        assert resp.json()["thread_id"] == thread["id"]

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


# ---------------------------------------------------------------------------
# Triggers, over the committed-migration-backed app
# ---------------------------------------------------------------------------


class TestTriggers:
    async def test_direct_wiring_fires_on_thread_create(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``threads`` fires its directly-wired trigger (``audit-log``)."""
        with caplog.at_level(logging.INFO, logger="webhooks_example.webhook"):
            resp = await client.post("/threads", json={"title": "Hello"})
        assert resp.status_code == 201
        assert "[webhook:audit-log]" in caplog.text

    async def test_config_driven_wiring_fires_on_message_create(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``messages`` fires its config-driven trigger (``slack-notify``)."""
        thread = await _make_thread(client)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="webhooks_example.webhook"):
            resp = await client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
        assert resp.status_code == 201
        assert "[webhook:slack-notify]" in caplog.text

    async def test_batch_edit_fires_once_for_the_whole_batch(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        thread = await _make_thread(client)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="webhooks_example.webhook"):
            resp = await client.post(
                "/messages/batch-edit",
                json=[
                    {"kind": "Create", "item": {"thread_id": thread["id"], "text": "a"}},
                    {"kind": "Create", "item": {"thread_id": thread["id"], "text": "b"}},
                ],
            )
        assert resp.status_code == 200
        webhook_records = [r for r in caplog.records if r.name == "webhooks_example.webhook"]
        assert len(webhook_records) == 1

    async def test_the_two_resources_fire_independently(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deleting a ``thread`` fires only ``audit-log``, never ``slack-notify``.

        ``threads``' trigger is wired directly on the resource; ``messages``'
        comes from the config-driven builder. Proving a ``threads`` write
        never touches ``slack-notify`` confirms the two rungs are genuinely
        independent, not merely coincidentally both configured.
        """
        thread = await _make_thread(client)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="webhooks_example.webhook"):
            resp = await client.delete(f"/threads/{thread['id']}")
        assert resp.status_code == 204
        assert "[webhook:audit-log]" in caplog.text
        assert "[webhook:slack-notify]" not in caplog.text
