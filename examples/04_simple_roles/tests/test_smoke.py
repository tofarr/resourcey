"""Smoke tests for the simple-roles example app.

These pin the app's factory wiring and the per-app role vocabulary, plus the
stored-principal wiring, against an in-memory SQLite database via httpx's ASGI
transport. The end-to-end behaviour lives in ``test_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.v2.auth.auth_api_key import (
    API_KEY_CHALLENGE,
    API_KEY_HEADER_NAME,
    ApiKeyAuthenticator,
)
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.auth.auth_role import AppRole, role_key
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.sql_resource import SqlResource
from simple_roles.app import Role, build_auth
from simple_roles.message import MessageResource
from simple_roles.models import Base, Message, Thread
from simple_roles.seed import ADMIN_ID, USER_ID, seed_users
from simple_roles.user import user_resource, user_view

ADMIN_KEY = "admin-key"
USER_KEY = "user-key"


def _keys() -> ApiKeysConfig:
    """A key list as the environment would supply it, with credential-carried roles."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(
                id="admin", key=SecretStr(ADMIN_KEY), principal_id=str(ADMIN_ID), roles=["ADMIN"]
            ),
            ApiKeyConfig(
                id="user", key=SecretStr(USER_KEY), principal_id=str(USER_ID), roles=["USER"]
            ),
        ]
    )


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_users(maker)

    users_inner = user_resource(session_factory=maker)
    builder, key_view = build_auth(_keys(), users=users_inner)
    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_factory=maker),
            MessageResource(Message, session_factory=maker),
            key_view,
            user_view(users_inner),
        ]
    )
    built: FastAPI = create_app(manifest, dependency_builder=builder)
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


def test_role_vocabulary_is_per_app_plain_strings() -> None:
    """The `Role` vocabulary names roles without magic strings; values are plain."""
    assert Role.ADMIN == "ADMIN"
    assert role_key(Role.USER) == "USER"
    assert issubclass(Role, AppRole)


def test_build_auth_wires_the_role_resolver() -> None:
    """The factory always wires the API-key authenticator and the role resolver."""
    users = user_resource(session_factory=None)
    builder, _view = build_auth(_keys(), users=users)
    assert isinstance(builder, AuthorizedDependencyBuilder)
    assert isinstance(builder.authenticator, ApiKeyAuthenticator)
    # The centralized resolver is the app's ``ROLE_POLICIES``.
    assert "ADMIN" in builder.policy_resolver.role_policies  # type: ignore[attr-defined]
    # The authenticator holds the principal store it validates keys against.
    assert builder.authenticator.user_resource is users
    # Reads are public, so the builder lets an absent credential through.
    assert builder.posture is Posture.OPTIONAL


async def test_anonymous_read_is_allowed_but_a_bad_key_is_rejected(client: AsyncClient) -> None:
    # Reads are public, so an absent credential is anonymous (200) while a
    # *presented but invalid* key is still a 401 — the two tiers now differ.
    assert (await client.get("/threads")).status_code == 200
    wrong = await client.get("/threads", headers={API_KEY_HEADER_NAME: "nope"})
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == API_KEY_CHALLENGE


async def test_user_resource_is_read_only_in_the_openapi_schema(client: AsyncClient) -> None:
    """The ``users`` view exposes no write route, so only reads are documented."""
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert set(paths["/users"]) == {"get"}
    assert set(paths["/users/{id}"]) == {"get"}


async def test_roles_are_not_leaked_on_the_key_resource(client: AsyncClient) -> None:
    """`roles` is hidden from the served key surface, like the digest."""
    resp = await client.get("/api-keys", headers={API_KEY_HEADER_NAME: ADMIN_KEY})
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "roles" not in item
        assert "key" not in item
