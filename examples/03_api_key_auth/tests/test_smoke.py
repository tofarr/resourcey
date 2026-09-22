"""Smoke tests for the API-key-auth example app.

The whole example is one posture: a single environment-configured API key
secures every resource. These tests pin the three outcomes a client can get —
correct key (allowed), incorrect key (403), missing key (403) — against an
in-memory SQLite database via httpx's ASGI transport, plus one happy-path CRUD
round trip to show the key gates a working API rather than a broken one.

The builder is selected exactly as ``.env`` selects it: by resolving
``FrameworkConfig.dependency_builder`` through the ``DEPENDENCY_BUILDER_CLASS``
env var, so the tests exercise the config-driven wiring, not a hand-injected
object.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from resourcey.app_context import AppContext
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import clear_config_cache
from resourcey.manifest import ResourceManifest
from resourcey.resource.sql import _SESSION_FACTORY_KEY, ResourceyBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api_key_auth.message import Message
from api_key_auth.thread import Thread

# The key the tests present; the same env var ``.env`` sets.
_API_KEY = "example-api-key"
_KEY_HEADER = "X-API-Key"


@pytest_asyncio.fixture
async def app(monkeypatch) -> AsyncIterator[FastAPI]:
    """The example app with an in-memory SQLite db and the env-selected builder."""
    monkeypatch.setenv(
        "DEPENDENCY_BUILDER_CLASS",
        "resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder",
    )
    monkeypatch.setenv("DEPENDENCY_BUILDER_API_KEYS_0", _API_KEY)
    # Each test resolves the builder from these vars rather than a cached config.
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()

    manifest = ResourceManifest(resources=(Thread, Message))
    manifest.materialize()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    ctx = AppContext(FrameworkConfig())
    ctx.set(_SESSION_FACTORY_KEY, factory)
    built = manifest.create_app(app_context=ctx)
    # ASGITransport does not run the lifespan; enter the manifest manually so
    # each instance's __aenter__ copies the pre-seeded factory.
    await manifest.__aenter__()
    yield built
    await manifest.__aexit__(None, None, None)
    await engine.dispose()
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_correct_key_allows_crud(client: AsyncClient) -> None:
    """A valid key in ``X-API-Key`` unlocks the full CRUD path."""
    headers = {_KEY_HEADER: _API_KEY}

    created = await client.post("/threads", json={"title": "Authed"}, headers=headers)
    assert created.status_code == 201, created.text
    thread = created.json()
    assert thread["title"] == "Authed"

    read = await client.get(f"/threads/{thread['id']}", headers=headers)
    assert read.status_code == 200
    assert read.json()["title"] == "Authed"

    listed = await client.get("/threads", headers=headers)
    assert listed.status_code == 200
    assert [t["title"] for t in listed.json()["items"]] == ["Authed"]

    deleted = await client.delete(f"/threads/{thread['id']}", headers=headers)
    assert deleted.status_code == 204


async def test_correct_key_via_bearer(client: AsyncClient) -> None:
    """The key is also accepted as ``Authorization: Bearer <key>``."""
    resp = await client.get("/threads", headers={"Authorization": f"Bearer {_API_KEY}"})
    assert resp.status_code == 200


async def test_incorrect_key_is_rejected(client: AsyncClient) -> None:
    """A wrong key is a 403 on read and on write alike."""
    headers = {_KEY_HEADER: "not-the-key"}

    read = await client.get("/threads", headers=headers)
    assert read.status_code == 403

    write = await client.post("/threads", json={"title": "Nope"}, headers=headers)
    assert write.status_code == 403


async def test_missing_key_is_rejected(client: AsyncClient) -> None:
    """No key at all is a 403 (fail-closed), for reads and writes."""
    read = await client.get("/threads")
    assert read.status_code == 403

    write = await client.post("/threads", json={"title": "Nope"})
    assert write.status_code == 403
