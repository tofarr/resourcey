"""End-to-end REST API tests for the simple-roles example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration itself is
verified.

The app is assembled through the real config path and httpx's ASGI transport, so
requests drive the full request → auth → role → service → SQLAlchemy stack.
The accepted keys carry roles, which is the point: no role lookup touches the
database, and the resolver's rules decide what each role may do.

Roles under test:

* ``ADMIN``  — full access everywhere.
* ``MODERATOR`` — read-only on ``threads``; full access on ``messages``.
* ``USER``   — read-only on ``threads``; **own rows only** on ``messages``.
* ``NOROLES`` — authenticates but every action is denied (fail-closed default).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from resourcey.v2.auth.auth_api_key import API_KEY_HEADER_NAME
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig
from simple_roles.app import build_app

ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

ADMIN_KEY = "admin-key"
MODERATOR_KEY = "moderator-key"
USER_KEY = "user-key"
NOROLES_KEY = "noroles-key"


def _keys() -> ApiKeysConfig:
    """The accepted keys as the environment supplies them (with their roles)."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="admin", key=SecretStr(ADMIN_KEY), roles=["ADMIN"]),
            ApiKeyConfig(id="moderator", key=SecretStr(MODERATOR_KEY), roles=["MODERATOR"]),
            ApiKeyConfig(
                id="user", key=SecretStr(USER_KEY), principal_id=str(ALICE), roles=["USER"]
            ),
            ApiKeyConfig(id="noroles", key=SecretStr(NOROLES_KEY)),
        ]
    )


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _sync_url(async_url: str) -> str:
    """Alembic drives a sync engine; mirror ``migrations/env.py``'s conversion."""
    return async_url.replace("+aiosqlite", "")


def _apply_migration(async_url: str) -> None:
    config = AlembicConfig()
    config.set_main_option("script_location", str(_migrations_dir()))
    config.set_main_option("sqlalchemy.url", _sync_url(async_url))
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A fully wired, role-checked v2 REST client backed by a migrated SQLite file."""
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager, keys=_keys())
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


def _h(key: str) -> dict[str, str]:
    return {API_KEY_HEADER_NAME: key}


async def _make_thread(client: AsyncClient, key: str, title: str = "T") -> dict[str, object]:
    resp = await client.post("/threads", json={"title": title}, headers=_h(key))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_message(
    client: AsyncClient, key: str, thread_id: object, text: str
) -> dict[str, object]:
    resp = await client.post(
        "/messages", json={"thread_id": thread_id, "text": text}, headers=_h(key)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Authentication posture (unchanged from example 03)
# ---------------------------------------------------------------------------


class TestPosture:
    async def test_missing_and_invalid_keys_are_rejected(self, client: AsyncClient) -> None:
        assert (await client.get("/threads")).status_code == 401
        assert (await client.get("/threads", headers=_h("nope"))).status_code == 401


# ---------------------------------------------------------------------------
# Role vocabulary is per-app and carried on the key with no DB lookup
# ---------------------------------------------------------------------------


class TestRoleCarriage:
    async def test_config_key_roles_authenticate_without_a_lookup(
        self, client: AsyncClient
    ) -> None:
        # A USER key reads the shared board — its role came off the key itself.
        assert (await client.get("/threads", headers=_h(USER_KEY))).status_code == 200


# ---------------------------------------------------------------------------
# The headline rule: a USER reads all of threads but only its own messages
# ---------------------------------------------------------------------------


class TestUserOwnRowsOnly:
    async def test_reads_all_threads(self, client: AsyncClient) -> None:
        await _make_thread(client, ADMIN_KEY, "T1")
        await _make_thread(client, ADMIN_KEY, "T2")
        resp = await client.get("/threads", headers=_h(USER_KEY))
        assert resp.status_code == 200
        assert {t["title"] for t in resp.json()["items"]} == {"T1", "T2"}

    async def test_cannot_write_threads(self, client: AsyncClient) -> None:
        resp = await client.post("/threads", json={"title": "Nope"}, headers=_h(USER_KEY))
        assert resp.status_code == 403

    async def test_reads_only_its_own_messages(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        await _make_message(client, USER_KEY, thread["id"], "mine")
        # The admin's message is owned by no one, but a USER still cannot see it.
        await _make_message(client, ADMIN_KEY, thread["id"], "theirs")

        resp = await client.get("/messages", headers=_h(USER_KEY))
        assert resp.status_code == 200
        texts = [m["text"] for m in resp.json()["items"]]
        assert texts == ["mine"]

    async def test_message_owner_is_stamped_server_side(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        created = await _make_message(client, USER_KEY, thread["id"], "mine")
        assert created["author_id"] == str(ALICE)

    async def test_cannot_read_another_owners_message_by_id(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        theirs = await _make_message(client, ADMIN_KEY, thread["id"], "theirs")
        resp = await client.get(f"/messages/{theirs['id']}", headers=_h(USER_KEY))
        assert resp.status_code == 404  # existence is not leaked

    async def test_cannot_update_another_owners_message(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        theirs = await _make_message(client, ADMIN_KEY, thread["id"], "theirs")
        resp = await client.patch(
            f"/messages/{theirs['id']}", json={"text": "hacked"}, headers=_h(USER_KEY)
        )
        assert resp.status_code == 404

    async def test_can_update_its_own_message(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        mine = await _make_message(client, USER_KEY, thread["id"], "old")
        resp = await client.patch(
            f"/messages/{mine['id']}", json={"text": "new"}, headers=_h(USER_KEY)
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "new"

    async def test_cannot_reassign_a_message_owner(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        mine = await _make_message(client, USER_KEY, thread["id"], "mine")
        # ``author_id`` is not a client field, so the forged value is ignored.
        resp = await client.patch(
            f"/messages/{mine['id']}",
            json={"text": "ok", "author_id": str(BOB)},
            headers=_h(USER_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["author_id"] == str(ALICE)


# ---------------------------------------------------------------------------
# ADMIN: full access
# ---------------------------------------------------------------------------


class TestAdmin:
    async def test_full_access_to_both_resources(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY, "Admin thread")
        msg = await _make_message(client, ADMIN_KEY, thread["id"], "admin msg")
        # Admin sees every message regardless of owner.
        resp = await client.get("/messages", headers=_h(ADMIN_KEY))
        assert {m["text"] for m in resp.json()["items"]} == {"admin msg"}
        # ...and can delete anyone's.
        assert (
            await client.delete(f"/messages/{msg['id']}", headers=_h(ADMIN_KEY))
        ).status_code == 204
        assert (
            await client.delete(f"/threads/{thread['id']}", headers=_h(ADMIN_KEY))
        ).status_code == 204


# ---------------------------------------------------------------------------
# MODERATOR: read-only on threads, full access on messages
# ---------------------------------------------------------------------------


class TestModerator:
    async def test_can_read_but_not_write_threads(self, client: AsyncClient) -> None:
        await _make_thread(client, ADMIN_KEY)
        assert (await client.get("/threads", headers=_h(MODERATOR_KEY))).status_code == 200
        resp = await client.post("/threads", json={"title": "N"}, headers=_h(MODERATOR_KEY))
        assert resp.status_code == 403

    async def test_can_edit_any_message(self, client: AsyncClient) -> None:
        thread = await _make_thread(client, ADMIN_KEY)
        mine = await _make_message(client, USER_KEY, thread["id"], "user msg")
        resp = await client.patch(
            f"/messages/{mine['id']}", json={"text": "moderated"}, headers=_h(MODERATOR_KEY)
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "moderated"


# ---------------------------------------------------------------------------
# An un-roled key authenticates but is denied everything (fail-closed)
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_unroled_key_cannot_read(self, client: AsyncClient) -> None:
        # A denied collection yields an empty page, not a 403 (union semantics).
        resp = await client.get("/threads", headers=_h(NOROLES_KEY))
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_unroled_key_cannot_create(self, client: AsyncClient) -> None:
        resp = await client.post("/threads", json={"title": "N"}, headers=_h(NOROLES_KEY))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The exposed key resource
# ---------------------------------------------------------------------------


class TestKeyResource:
    async def test_key_list_hides_the_digest(self, client: AsyncClient) -> None:
        resp = await client.get("/api-keys", headers=_h(ADMIN_KEY))
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "key" not in item
