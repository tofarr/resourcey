"""End-to-end REST API tests for the full-RBAC example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration itself is
verified, then the RBAC tables are seeded through the real store models.

The app is assembled through the real config path and httpx's ASGI transport, so
requests drive the full request → auth → **resolve** → service → SQLAlchemy
stack. The accepted keys carry only a ``principal_id``; the roles, groups and
permissions all live in the store and are resolved **per request**.

Roles under test (seeded by ``full_rbac.seed``):

* ``admin``  — full access to ``threads`` and ``messages``.
* ``viewer`` — read-only on both.
* ``author`` — read-only on ``threads``; **own rows only** on ``messages``.
* (a user with no grants authenticates but is denied everything — fail-closed.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from full_rbac.app import build_app
from full_rbac.seed import (
    ADMIN_USER,
    AUTHOR_USER,
    AUTHORS,
    VIEWER_USER,
    VIEWERS,
    seed,
)
from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_policy import DenyAll
from resourcey.auth.auth_rbac import (
    GroupRole,
    GroupUser,
    Role,
    RolePermission,
    User,
    policy_to_json,
)
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig

ADMIN_KEY = "admin-key"
VIEWER_KEY = "viewer-key"
AUTHOR_KEY = "author-key"
UNGRANTED = UUID("00000000-0000-0000-0000-0000000000d0")
UNGRANTED_KEY = "ungranted-key"


def _keys() -> ApiKeysConfig:
    """The accepted keys, each bound to a stored user id (no credential-carried roles)."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="admin", key=SecretStr(ADMIN_KEY), principal_id=str(ADMIN_USER)),
            ApiKeyConfig(id="viewer", key=SecretStr(VIEWER_KEY), principal_id=str(VIEWER_USER)),
            ApiKeyConfig(id="author", key=SecretStr(AUTHOR_KEY), principal_id=str(AUTHOR_USER)),
            ApiKeyConfig(id="ungranted", key=SecretStr(UNGRANTED_KEY), principal_id=str(UNGRANTED)),
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


@dataclass
class Env:
    """The running app and the session maker over the same (isolated) database."""

    client: AsyncClient
    maker: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch) -> AsyncIterator[Env]:
    """A fully wired, store-resolved app backed by a migrated SQLite file."""
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
        maker = await manager.get_session_maker()
        await seed(maker)
        # A user with no groups at all: authenticates, resolves to no permission.
        async with maker() as session:
            session.add(User(id=UNGRANTED, email="none@example.com", username="none"))
            await session.commit()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield Env(client=client, maker=maker)
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
# Authentication posture
# ---------------------------------------------------------------------------


class TestPosture:
    async def test_missing_and_invalid_keys_are_rejected(self, env: Env) -> None:
        assert (await env.client.get("/threads")).status_code == 401
        assert (await env.client.get("/threads", headers=_h("nope"))).status_code == 401


# ---------------------------------------------------------------------------
# Resolution is store-backed: the credential carries only a principal id
# ---------------------------------------------------------------------------


class TestStoreResolution:
    async def test_admin_resolves_full_access_from_the_store(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY, "Admin thread")
        msg = await _make_message(env.client, ADMIN_KEY, thread["id"], "admin msg")
        assert (await env.client.get("/messages", headers=_h(ADMIN_KEY))).json()["items"]
        assert (
            await env.client.delete(f"/messages/{msg['id']}", headers=_h(ADMIN_KEY))
        ).status_code == 204

    async def test_ungranted_user_is_fail_closed(self, env: Env) -> None:
        # A denied collection read is emptied, not a 403 (union semantics).
        resp = await env.client.get("/threads", headers=_h(UNGRANTED_KEY))
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert (
            await env.client.post("/threads", json={"title": "N"}, headers=_h(UNGRANTED_KEY))
        ).status_code == 403


# ---------------------------------------------------------------------------
# Viewer: read-only on both resources
# ---------------------------------------------------------------------------


class TestViewer:
    async def test_reads_but_cannot_write_threads(self, env: Env) -> None:
        await _make_thread(env.client, ADMIN_KEY)
        assert (await env.client.get("/threads", headers=_h(VIEWER_KEY))).status_code == 200
        assert (
            await env.client.post("/threads", json={"title": "N"}, headers=_h(VIEWER_KEY))
        ).status_code == 403

    async def test_reads_every_message_but_cannot_write(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        await _make_message(env.client, ADMIN_KEY, thread["id"], "theirs")
        resp = await env.client.get("/messages", headers=_h(VIEWER_KEY))
        assert [m["text"] for m in resp.json()["items"]] == ["theirs"]
        assert (
            await env.client.post(
                "/messages",
                json={"thread_id": thread["id"], "text": "no"},
                headers=_h(VIEWER_KEY),
            )
        ).status_code == 403


# ---------------------------------------------------------------------------
# Author: the headline rule — read all of threads, own rows only of messages
# ---------------------------------------------------------------------------


class TestAuthorOwnRowsOnly:
    async def test_reads_all_threads(self, env: Env) -> None:
        await _make_thread(env.client, ADMIN_KEY, "T1")
        await _make_thread(env.client, ADMIN_KEY, "T2")
        resp = await env.client.get("/threads", headers=_h(AUTHOR_KEY))
        assert {t["title"] for t in resp.json()["items"]} == {"T1", "T2"}

    async def test_reads_only_its_own_messages(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        await _make_message(env.client, AUTHOR_KEY, thread["id"], "mine")
        await _make_message(env.client, ADMIN_KEY, thread["id"], "theirs")
        resp = await env.client.get("/messages", headers=_h(AUTHOR_KEY))
        assert [m["text"] for m in resp.json()["items"]] == ["mine"]

    async def test_message_owner_is_stamped_server_side(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        created = await _make_message(env.client, AUTHOR_KEY, thread["id"], "mine")
        assert created["author_id"] == str(AUTHOR_USER)

    async def test_cannot_read_another_owners_message_by_id(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        theirs = await _make_message(env.client, ADMIN_KEY, thread["id"], "theirs")
        assert (
            await env.client.get(f"/messages/{theirs['id']}", headers=_h(AUTHOR_KEY))
        ).status_code == 404

    async def test_cannot_update_another_owners_message(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        theirs = await _make_message(env.client, ADMIN_KEY, thread["id"], "theirs")
        resp = await env.client.patch(
            f"/messages/{theirs['id']}", json={"text": "hacked"}, headers=_h(AUTHOR_KEY)
        )
        assert resp.status_code == 404  # 404, not 403 — existence is not leaked

    async def test_can_update_its_own_message(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        mine = await _make_message(env.client, AUTHOR_KEY, thread["id"], "old")
        resp = await env.client.patch(
            f"/messages/{mine['id']}", json={"text": "new"}, headers=_h(AUTHOR_KEY)
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "new"

    async def test_cannot_reassign_a_message_owner(self, env: Env) -> None:
        thread = await _make_thread(env.client, ADMIN_KEY)
        mine = await _make_message(env.client, AUTHOR_KEY, thread["id"], "mine")
        resp = await env.client.patch(
            f"/messages/{mine['id']}",
            json={"text": "ok", "author_id": str(VIEWER_USER)},
            headers=_h(AUTHOR_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["author_id"] == str(AUTHOR_USER)


# ---------------------------------------------------------------------------
# Multiple roles OR-combine (union model, no deny-wins override)
# ---------------------------------------------------------------------------


class TestUnionOfRoles:
    async def test_two_roles_union_their_grants(self, env: Env) -> None:
        """A principal in two groups unions both roles' policies: read all, edit own."""
        async with env.maker() as session:
            session.add(GroupUser(id=uuid4(), group_id=AUTHORS, user_id=VIEWER_USER))
            await session.commit()
        thread = await _make_thread(env.client, ADMIN_KEY)
        mine = await _make_message(env.client, VIEWER_KEY, thread["id"], "mine")
        theirs = await _make_message(env.client, ADMIN_KEY, thread["id"], "theirs")
        # viewer (read-all) ∪ author (own-rows) => reads every message...
        resp = await env.client.get("/messages", headers=_h(VIEWER_KEY))
        assert {m["text"] for m in resp.json()["items"]} == {"mine", "theirs"}
        # ...but may only edit its own.
        assert (
            await env.client.patch(
                f"/messages/{mine['id']}", json={"text": "ok"}, headers=_h(VIEWER_KEY)
            )
        ).status_code == 200
        assert (
            await env.client.patch(
                f"/messages/{theirs['id']}", json={"text": "no"}, headers=_h(VIEWER_KEY)
            )
        ).status_code == 404

    async def test_denyall_does_not_override_a_grant(self, env: Env) -> None:
        """A second role on the same group carrying ``DenyAll`` never suppresses a grant."""
        denied_id = uuid4()
        async with env.maker() as session:
            session.add(Role(id=denied_id, name="denier"))
            session.add(GroupRole(id=uuid4(), group_id=VIEWERS, role_id=denied_id))
            session.add(
                RolePermission(
                    id=uuid4(),
                    role_id=denied_id,
                    resource="messages",
                    permission=policy_to_json(DenyAll()),
                )
            )
            await session.commit()
        thread = await _make_thread(env.client, ADMIN_KEY)
        await _make_message(env.client, ADMIN_KEY, thread["id"], "visible")
        # viewer (ReadOnly) ∪ denier (DenyAll) => the grant survives.
        resp = await env.client.get("/messages", headers=_h(VIEWER_KEY))
        assert [m["text"] for m in resp.json()["items"]] == ["visible"]

    async def test_group_membership_is_required_not_just_a_role(self, env: Env) -> None:
        """A user not in the role's group does not inherit its permission."""
        # The ungranted user is added to the users table (no group), so no role.
        assert (await env.client.get("/roles", headers=_h(UNGRANTED_KEY))).status_code == 200
        assert (await env.client.get("/roles", headers=_h(UNGRANTED_KEY))).json()["items"] == []


# ---------------------------------------------------------------------------
# Membership change is honoured (no cache_ttl => immediate)
# ---------------------------------------------------------------------------


class TestMembershipChange:
    async def test_adding_a_membership_grants_access(self, env: Env) -> None:
        """A membership added after startup takes effect immediately (no cache_ttl)."""
        assert (await env.client.get("/threads", headers=_h(UNGRANTED_KEY))).json()["items"] == []
        async with env.maker() as session:
            session.add(GroupUser(id=uuid4(), group_id=VIEWERS, user_id=UNGRANTED))
            await session.commit()
        assert (await env.client.get("/threads", headers=_h(UNGRANTED_KEY))).status_code == 200


# ---------------------------------------------------------------------------
# The full RBAC resource set is served over the ordinary surface
# ---------------------------------------------------------------------------


class TestRbacResources:
    async def test_admin_can_administer_the_rbac_tables(self, env: Env) -> None:
        # The admin role also has full access to the RBAC resource set itself.
        resp = await env.client.get("/roles", headers=_h(ADMIN_KEY))
        assert resp.status_code == 200
        assert {r["name"] for r in resp.json()["items"]} >= {"admin", "viewer", "author"}


# ---------------------------------------------------------------------------
# The exposed key resource hides credential bindings
# ---------------------------------------------------------------------------


class TestKeyResource:
    async def test_key_list_hides_the_digest(self, env: Env) -> None:
        resp = await env.client.get("/api-keys", headers=_h(ADMIN_KEY))
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert "key" not in item
            assert "principal_id" not in item
