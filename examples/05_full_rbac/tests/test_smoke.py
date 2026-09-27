"""Smoke tests for the full-RBAC app factory and posture guard.

These pin the wiring (the store-backed resolver, the API-key authenticator, the
full RBAC resource set) and the fail-closed posture guard, against an in-memory
SQLite schema built with ``create_all`` and the RBAC tables seeded. End-to-end
behaviour lives in ``test_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from full_rbac.app import _verify_posture, build_app, build_auth, manager_session_source
from full_rbac.models import Base
from full_rbac.seed import ADMIN_USER, VIEWER_USER, seed
from resourcey.v2.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.auth.auth_rbac import RBAC_MODELS
from resourcey.v2.auth.auth_rbac_resolver import RbacPolicyResolver
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.http.dependency_builder import DefaultDependencyBuilder
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig


def _keys() -> ApiKeysConfig:
    """The accepted keys, each bound to a stored user id (no credential-carried roles)."""
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="admin", key=SecretStr("admin-key"), principal_id=str(ADMIN_USER)),
            ApiKeyConfig(id="viewer", key=SecretStr("viewer-key"), principal_id=str(VIEWER_USER)),
        ]
    )


async def _create_schema(maker: async_sessionmaker[AsyncSession]) -> None:
    """Create every table in the example's metadata over the manager's engine."""
    engine = maker.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A full-RBAC app over an in-memory SQLite schema with the RBAC tables seeded."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager, keys=_keys())
    await manifest.__aenter__()
    try:
        maker = await manager.get_session_maker()
        await _create_schema(maker)
        await seed(maker)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


def test_build_app_registers_the_rbac_resource_set() -> None:
    """The factory exposes the board *and* the full RBAC resource set."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, _app = build_app(session_manager=manager, keys=_keys())
    paths = {r.get_resource_path() for r in manifest.resources}
    assert {
        "threads",
        "messages",
        "users",
        "groups",
        "group-users",
        "roles",
        "group-roles",
        "role-permissions",
        "resource-acls",
    } <= paths
    assert len(RBAC_MODELS) == 7


def test_posture_guard_rejects_a_non_api_key_builder() -> None:
    """The guard refuses the no-auth default, so the app cannot be silently open."""
    with pytest.raises(ResourceyConfigError, match="API-key posture"):
        _verify_posture(DefaultDependencyBuilder())


def test_build_auth_resolver_is_the_rbac_resolver() -> None:
    """The store-backed RBAC resolver is what the builder carries."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    builder, _view = build_auth(manager_session_source(manager), _keys())
    assert isinstance(builder, AuthorizedDependencyBuilder)
    assert isinstance(builder.authenticator, ApiKeyAuthenticator)
    assert isinstance(builder.policy_resolver, RbacPolicyResolver)


async def test_missing_and_invalid_keys_are_rejected(client: AsyncClient) -> None:
    assert (await client.get("/threads")).status_code == 401
    assert (await client.get("/threads", headers={"X-API-Key": "nope"})).status_code == 401


async def test_seeded_admin_resolves_and_reads(client: AsyncClient) -> None:
    """The seeded admin's group -> role -> permission resolves to a readable board."""
    resp = await client.get("/threads", headers={"X-API-Key": "admin-key"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []
