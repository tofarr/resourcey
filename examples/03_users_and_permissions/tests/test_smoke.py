"""Smoke test for the users-and-permissions example app.

Exercises the full secured HTTP path against an in-memory SQLite database:
dev IdP login (cookie) → permission-enforced create / read / update / delete
for the admin (Permitted on everything) and a regular user (CreatorPermission
on their own threads, messages, and user record). Everything is denied
without an explicit ``UserPermission`` row (fail-closed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from resourcey.app_context import AppContext
from resourcey.auth.auth_models import AuthBase
from resourcey.config.config_framework import FrameworkConfig
from resourcey.resource.sql import _SESSION_FACTORY_KEY, ResourceyBase
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from users_and_permissions.app import app as _app_module_app  # noqa: F401 (imports manifest)
from users_and_permissions.app import manifest
from users_and_permissions.user import User
from users_and_permissions.user_permission import UserPermission


def _combined_metadata() -> MetaData:
    """Merge ResourceyBase + AuthBase tables (ResourceyBase wins collisions)."""
    combined = MetaData()
    for table in ResourceyBase.metadata.tables.values():
        table.to_metadata(combined)
    for name, table in AuthBase.metadata.tables.items():
        if name not in combined.tables:
            table.to_metadata(combined)
    return combined


async def _seed(engine) -> None:
    """Seed the admin + regular user and their permission rows.

    Inserts the ``users`` rows via the resource-generated ORM model (which
    declares ``creator_id``) so SQLAlchemy's ``Uuid`` type processors bind the
    ids correctly — raw SQL text binds would leave them as bare strings that
    the ORM's ``Uuid`` comparator cannot match on read.
    """
    import uuid
    from datetime import UTC, datetime

    from resourcey.auth.permission import CreatorPermission, Permitted

    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    regular_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    now = datetime.now(UTC)
    admin_hash = _hash_password("admin")
    regular_hash = _hash_password("regular")

    # The resource-generated ``User`` model (ResourceyBase) declares every
    # column the auth ``User`` model has, plus ``creator_id``.
    user_model = User.get_sql_alchemy_model()
    up_model = UserPermission.get_sql_alchemy_model()

    async with factory() as session:
        await session.execute(
            user_model.__table__.insert().values(
                id=admin_id,
                email="admin@example.com",
                username="admin",
                enabled=True,
                password=admin_hash,
                creator_id=admin_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            user_model.__table__.insert().values(
                id=regular_id,
                email="regular@example.com",
                username="regular",
                enabled=True,
                password=regular_hash,
                creator_id=regular_id,
                created_at=now,
                updated_at=now,
            )
        )

        admin_perm = Permitted().model_dump(mode="json")
        own = CreatorPermission(on_match=Permitted(), on_create=Permitted()).model_dump(
            mode="json"
        )
        rows: list[dict] = []
        for rt in ("Thread", "Message", "User", "UserPermission"):
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "user_id": admin_id,
                    "resource_type": rt,
                    "permission": admin_perm,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        for rt in ("Thread", "Message", "User"):
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "user_id": regular_id,
                    "resource_type": rt,
                    "permission": own,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        await session.execute(up_model.__table__.insert(), rows)
        await session.commit()


def _hash_password(plaintext: str) -> str:
    from resourcey.auth.password import hash_password

    return hash_password(plaintext)


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    """Assemble the secured app with an in-memory SQLite database + seed."""
    from resourcey.config.config_runtime import set_config

    cfg = FrameworkConfig()
    cfg.auth.cookie_secure = False
    cfg.auth.cookie_samesite = "lax"
    cfg.base_url = "http://localhost:8083"
    set_config(cfg)

    manifest.materialize()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_combined_metadata().create_all)
    await _seed(engine)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    ctx = AppContext(cfg)
    ctx.set(_SESSION_FACTORY_KEY, factory)
    built = manifest.create_app(app_context=ctx)
    # The auth session dependency reads the session factory from
    # ``app.state.resourcey_session_factory``; seed it so the dev IdP shares
    # the in-memory engine (it would otherwise build a Postgres engine from
    # the default config).
    built.state.resourcey_session_factory = factory
    built.state.resourcey_engine = engine
    # Wire the federated OAuth + dev IdP routers so the login flow mints the
    # session cookie the secured services resolve into a principal.
    from resourcey.auth.auth_router import router as auth_router
    from resourcey.auth.dev_router import router as dev_router

    built.include_router(auth_router)
    built.include_router(dev_router)
    # ASGITransport does not run the lifespan; enter the manifest manually so
    # each instance's __aenter__ copies the pre-seeded factory.
    await manifest.__aenter__()
    yield built
    await manifest.__aexit__(None, None, None)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client: AsyncClient, username: str, password: str) -> None:
    """Log in via the dev IdP so the client holds the session cookie."""
    resp = await client.post(
        "/auth/dev/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text


async def _bearer(client: AsyncClient, username: str, password: str) -> str:
    """Log in and return the raw cookie value as a Bearer token."""
    resp = await client.post(
        "/auth/dev/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    from resourcey.config.config_runtime import get_config_as

    cookie_name = get_config_as(FrameworkConfig).auth.cookie_name
    return resp.cookies[cookie_name]


async def test_anonymous_denied_everywhere(client: AsyncClient) -> None:
    """No credential → no access (fail-closed)."""
    resp = await client.get("/threads")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    resp = await client.post("/threads", json={"title": "nope"})
    assert resp.status_code == 403


async def test_admin_can_create_and_read_threads(client: AsyncClient) -> None:
    """Admin (Permitted) can create, read, update, delete anything."""
    await _login(client, "admin", "admin")
    resp = await client.post("/threads", json={"title": "Admin thread", "description": "hi"})
    assert resp.status_code == 201, resp.text
    thread = resp.json()
    assert thread["title"] == "Admin thread"
    assert thread["creator_id"] == "00000000-0000-0000-0000-000000000001"

    resp = await client.get(f"/threads/{thread['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Admin thread"

    resp = await client.patch(f"/threads/{thread['id']}", json={"title": "Edited"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Edited"

    resp = await client.delete(f"/threads/{thread['id']}")
    assert resp.status_code == 204


async def test_regular_user_can_create_own_thread(client: AsyncClient) -> None:
    """Regular user (CreatorPermission) can create + read their own thread."""
    await _login(client, "regular", "regular")
    resp = await client.post("/threads", json={"title": "My thread"})
    assert resp.status_code == 201, resp.text
    thread = resp.json()
    assert thread["creator_id"] == "00000000-0000-0000-0000-000000000002"

    resp = await client.get(f"/threads/{thread['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "My thread"


async def test_regular_user_cannot_update_others_thread(client: AsyncClient) -> None:
    """A regular user's update on an admin-owned thread is a 404 (no leak)."""
    # Admin creates a thread.
    await _login(client, "admin", "admin")
    resp = await client.post("/threads", json={"title": "Admin's thread"})
    thread_id = resp.json()["id"]

    # Regular user logs in fresh (different cookie).
    await _login(client, "regular", "regular")
    resp = await client.patch(f"/threads/{thread_id}", json={"title": "hijack"})
    assert resp.status_code == 404


async def test_regular_user_search_filters_to_own(client: AsyncClient) -> None:
    """CreatorPermission search narrows to the principal's own items."""
    await _login(client, "admin", "admin")
    await client.post("/threads", json={"title": "Admin thread"})

    await _login(client, "regular", "regular")
    await client.post("/threads", json={"title": "Regular thread"})

    resp = await client.get("/threads")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()["items"]}
    assert titles == {"Regular thread"}


async def test_message_creator_scoping(client: AsyncClient) -> None:
    """Messages inherit creator_id from the principal and scope to it."""
    await _login(client, "admin", "admin")
    t = (await client.post("/threads", json={"title": "T"})).json()
    m = (await client.post("/messages", json={"thread_id": t["id"], "text": "hi"})).json()
    assert m["creator_id"] == "00000000-0000-0000-0000-000000000001"

    # Regular user cannot read admin's message (CreatorPermission → 404).
    await _login(client, "regular", "regular")
    resp = await client.get(f"/messages/{m['id']}")
    assert resp.status_code == 404

    # But can create their own in the same thread.
    own = (await client.post("/messages", json={"thread_id": t["id"], "text": "mine"})).json()
    resp = await client.get(f"/messages/{own['id']}")
    assert resp.status_code == 200
    assert resp.json()["text"] == "mine"


async def test_regular_user_can_edit_own_user_record(client: AsyncClient) -> None:
    """CreatorPermission on User lets a user edit their own record."""
    await _login(client, "regular", "regular")
    regular_id = "00000000-0000-0000-0000-000000000002"
    resp = await client.patch(f"/users/{regular_id}", json={"email": "regular2@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "regular2@example.com"

    # Cannot edit the admin's record (not their own).
    resp = await client.patch(
        "/users/00000000-0000-0000-0000-000000000001", json={"email": "x@example.com"}
    )
    assert resp.status_code == 404


async def test_regular_user_cannot_read_user_permissions(client: AsyncClient) -> None:
    """The regular user has no UserPermission grant → denied (fail-closed).

    Search narrows to an empty page (200 with no items); create is denied
    outright (403) because the permission filter is ``NoneSearchFilter``.
    """
    await _login(client, "regular", "regular")
    resp = await client.get("/user-permissions")
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    resp = await client.post(
        "/user-permissions",
        json={
            "user_id": "00000000-0000-0000-0000-000000000002",
            "resource_type": "Thread",
            "permission": {"kind": "Permitted"},
        },
    )
    assert resp.status_code == 403


async def test_admin_can_list_user_permissions(client: AsyncClient) -> None:
    """Admin (Permitted on UserPermission) can list every permission row."""
    await _login(client, "admin", "admin")
    resp = await client.get("/user-permissions")
    assert resp.status_code == 200
    # 4 admin + 3 regular = 7 rows.
    assert len(resp.json()["items"]) == 7


async def test_bearer_token_works(client: AsyncClient) -> None:
    """The dev IdP cookie also works as a Bearer header."""
    token = await _bearer(client, "admin", "admin")
    resp = await client.get(
        "/threads", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
