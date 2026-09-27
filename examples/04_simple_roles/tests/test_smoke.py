"""Smoke tests for the simple-roles example app.

These pin the app's factory wiring and the per-app role vocabulary, plus the
fail-closed posture guard, against an in-memory SQLite database via httpx's ASGI
transport. The end-to-end behaviour lives in ``test_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
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
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.auth.auth_role import AppRole, role_key
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.http.dependency_builder import DefaultDependencyBuilder
from resourcey.v2.sql.sql_resource import SqlResource
from simple_roles.app import Role, _verify_posture, build_auth
from simple_roles.message import MessageResource
from simple_roles.models import Base, Message, Thread

ADMIN_KEY = "admin-key"
USER_KEY = "user-key"


def _keys() -> ApiKeysConfig:
    """A key list as the environment would supply it, with credential-carried roles."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="admin", key=SecretStr(ADMIN_KEY), roles=["ADMIN"]),
            ApiKeyConfig(id="user", key=SecretStr(USER_KEY), roles=["USER"]),
        ]
    )


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    builder, key_view = build_auth(_keys())
    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_factory=maker),
            MessageResource(Message, session_factory=maker),
            key_view,
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
    builder, _view = build_auth(_keys())
    assert isinstance(builder, AuthorizedDependencyBuilder)
    assert isinstance(builder.authenticator, ApiKeyAuthenticator)
    # The centralized resolver is the app's ``ROLE_POLICIES``.
    assert "ADMIN" in builder.policy_resolver.role_policies  # type: ignore[attr-defined]


def test_posture_guard_rejects_a_non_api_key_builder() -> None:
    """The guard refuses the no-auth default, so the app cannot be silently open."""
    with pytest.raises(ResourceyConfigError, match="API-key posture"):
        _verify_posture(DefaultDependencyBuilder())


async def test_absent_and_invalid_keys_are_indistinguishable(client: AsyncClient) -> None:
    missing = await client.get("/threads")
    wrong = await client.get("/threads", headers={API_KEY_HEADER_NAME: "nope"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert wrong.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert missing.json() == wrong.json()


async def test_roles_are_not_leaked_on_the_key_resource(client: AsyncClient) -> None:
    """`roles` is hidden from the served key surface, like the digest."""
    resp = await client.get("/api-keys", headers={API_KEY_HEADER_NAME: ADMIN_KEY})
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "roles" not in item
        assert "key" not in item
