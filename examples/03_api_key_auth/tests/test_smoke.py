"""Smoke tests for the API-key-auth example app.

The whole example is one posture: environment-configured API keys secure every
resource. These tests pin the outcomes a client can get — correct key (allowed),
incorrect key (401), missing key (401), an empty key list (fail-closed) — against
an in-memory SQLite database via httpx's ASGI transport, plus one happy-path CRUD
round trip to show the key gates a working API rather than a broken one.

The app is assembled through :func:`api_key_auth.app.build_auth` and the real
``create_app`` wiring, so the tests exercise the shipped posture rather than a
hand-injected dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api_key_auth.app import build_auth
from api_key_auth.message import MessageResource
from api_key_auth.models import Base, Message, Thread
from resourcey.auth.auth_api_key import (
    API_KEY_CHALLENGE,
    API_KEY_HEADER_NAME,
    ApiKeyAuthenticator,
)
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_me_routes import register_me_routes
from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.sql.sql_resource import SqlResource

# The key the tests present; the same value ``.env`` sets.
_API_KEY = "example-api-key"


def _keys(*values: str) -> ApiKeysConfig:
    """A key list as the environment would supply it (ids are placeholders)."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(
                id=f"k{i}",
                name=None,
                key=SecretStr(value),
                principal_id="00000000-0000-0000-0000-000000000001",
            )
            for i, value in enumerate(values)
        ]
    )


def _manifest(session_factory: Any, keys: ApiKeysConfig) -> tuple[Manifest, Any]:
    """The example's resources over an injected session factory and key list."""
    builder, key_view = build_auth(keys)
    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_factory=session_factory),
            MessageResource(Message, session_factory=session_factory),
            key_view,
        ]
    )
    return manifest, builder


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    """The example app over a shared in-memory SQLite db and a known key list."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    manifest, builder = _manifest(maker, _keys(_API_KEY))
    built: FastAPI = create_app(manifest, dependency_builder=builder)
    # Mirror the shipped `build_app`: mount the `me` endpoint over the same
    # authenticator (no user store in this example).
    register_me_routes(built, authenticator=builder.authenticator)
    # ASGITransport does not run the lifespan; enter the manifest manually so the
    # resources' runtime lifecycle is active for the requests below.
    await manifest.__aenter__()
    try:
        yield built
    finally:
        await manifest.__aexit__(None, None, None)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_correct_key_allows_crud(client: AsyncClient) -> None:
    """A valid key in ``X-API-Key`` unlocks the full CRUD path."""
    headers = {API_KEY_HEADER_NAME: _API_KEY}

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
    """A wrong key is a 401 on read and on write alike."""
    headers = {API_KEY_HEADER_NAME: "not-the-key"}

    read = await client.get("/threads", headers=headers)
    assert read.status_code == 401

    write = await client.post("/threads", json={"title": "Nope"}, headers=headers)
    assert write.status_code == 401


async def test_missing_key_is_rejected(client: AsyncClient) -> None:
    """No key at all is a 401 (fail-closed), for reads and writes."""
    read = await client.get("/threads")
    assert read.status_code == 401

    write = await client.post("/threads", json={"title": "Nope"})
    assert write.status_code == 401


async def test_absent_and_invalid_keys_are_indistinguishable(client: AsyncClient) -> None:
    """A 401 carries a ``WWW-Authenticate`` challenge and no key-vs-missing tell."""
    missing = await client.get("/threads")
    wrong = await client.get("/threads", headers={API_KEY_HEADER_NAME: "not-the-key"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert wrong.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert missing.json() == wrong.json()


async def test_empty_key_list_denies_everything() -> None:
    """An empty configured key list fails closed rather than opening the API."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    manifest, builder = _manifest(maker, _keys())
    built: FastAPI = create_app(manifest, dependency_builder=builder)
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=built)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Even the correct key is rejected when the accepted list is empty.
            resp = await c.get("/threads", headers={API_KEY_HEADER_NAME: _API_KEY})
            assert resp.status_code == 401
    finally:
        await manifest.__aexit__(None, None, None)
        await engine.dispose()


def test_build_auth_wires_the_api_key_builder() -> None:
    """The factory always builds the API-key authenticator."""
    builder, _view = build_auth(_keys(_API_KEY))
    assert isinstance(builder, AuthorizedDependencyBuilder)
    assert isinstance(builder.authenticator, ApiKeyAuthenticator)


async def test_me_returns_the_service_principal(client: AsyncClient) -> None:
    """`GET /me` (issue #150) returns the authenticated key's principal.

    This example has no user store, so the body carries only the principal; the
    config key's PRINCIPAL_ID gives `sub` a stable value.
    """
    resp = await client.get("/me", headers={API_KEY_HEADER_NAME: _API_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sub"] == "00000000-0000-0000-0000-000000000001"
    assert body["kind"] == "service"


async def test_me_requires_a_credential(client: AsyncClient) -> None:
    assert (await client.get("/me")).status_code == 401
    assert (await client.get("/me", headers={API_KEY_HEADER_NAME: "nope"})).status_code == 401
