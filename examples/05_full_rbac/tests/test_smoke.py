"""Smoke tests for the full-RBAC app factory.

These pin the wiring (the store-backed resolver, the stored-API-key
authenticator, the full RBAC resource set) against an in-memory SQLite schema
built with ``create_all`` and the store seeded. End-to-end behaviour lives in
``test_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from full_rbac.app import API_KEY_PATH, build_app, build_auth
from full_rbac.models import Base
from full_rbac.seed import VIEWER_USER, seed
from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.auth.auth_rbac import RBAC_MODELS
from resourcey.auth.auth_rbac_resolver import RbacPolicyResolver
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig


async def _create_schema(maker: async_sessionmaker[AsyncSession]) -> None:
    """Create every table in the example's metadata over the manager's engine."""
    engine = maker.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A full-RBAC app over an in-memory SQLite schema with the RBAC tables seeded."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager)
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
    manifest, _app = build_app(session_manager=manager)
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


def test_the_admin_grant_names_the_served_key_path() -> None:
    """``API_KEY_PATH`` matches the path the key resource is actually served at.

    A grant on a path nothing serves would leave the key table ungoverned, so
    this pins the constant against the resource builder's own default.
    """
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, _app = build_app(session_manager=manager)
    assert API_KEY_PATH in {r.get_resource_path() for r in manifest.resources}


def test_build_auth_resolver_is_the_rbac_resolver() -> None:
    """The store-backed RBAC resolver is what the builder carries."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    builder, _view = build_auth(manager)
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


async def test_me_reads_the_callers_own_stored_row(client: AsyncClient) -> None:
    """`me` (issue #150) reads the caller's own `users` row directly, enriched
    with the stored email / username, independent of the `users` policy."""
    body = (await client.get("/me", headers={"X-API-Key": "viewer-key"})).json()
    assert body["sub"] == str(VIEWER_USER)
    assert body["email"] == "viewer@example.com"
    assert body["preferred_username"] == "viewer"


async def test_me_requires_a_credential(client: AsyncClient) -> None:
    assert (await client.get("/me")).status_code == 401
    assert (await client.get("/me", headers={"X-API-Key": "nope"})).status_code == 401
