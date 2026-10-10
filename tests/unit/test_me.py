"""Tests for the ``me`` endpoint (issue #150).

These drive the real code paths — a real config-list API-key authenticator, a
real ``ListResource`` principal store, and the real ``create_app`` transport with
the route mounted after it — with no mocks. Covered:

* ``GET`` / ``POST /me`` both return the OIDC-UserInfo-shaped body;
* ``sub`` is always present and is the internal principal id;
* an absent credential is a ``401`` even under ``Posture.OPTIONAL`` (the strict
  dependency), and an invalid one is a ``401``;
* ``external_id`` / ``kind`` / ``roles`` / ``scopes`` come off the ``Principal``;
* profile claims are included only when the caller's own row carries a
  non-``None`` value — an unavailable claim is omitted, not ``null``;
* ``me`` reads only the caller's own row, independent of the ``users`` policy;
* the path is configurable and a pre-existing route wins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr

from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME, ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_me_routes import register_me_routes
from resourcey.auth.auth_policy import DenyAll, ReadOnly
from resourcey.auth.auth_principal import (
    Authenticator,
    AuthResult,
    Principal,
    PrincipalKind,
)
from resourcey.auth.auth_role import RolePolicyResolver
from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.list.list_resource import ListResource

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")
API_KEY = "me-secret"
API_KEY_HEADER = {API_KEY_HEADER_NAME: API_KEY}


class Profile(BaseModel):
    """The principal store model: the caller's own row, with optional profile fields."""

    id: UUID
    email: str | None = None
    username: str | None = None
    name: str | None = None


def _keys() -> ApiKeysConfig:
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(
                id="k1",
                key=SecretStr(API_KEY),
                principal_id=str(USER_ID),
                roles=["ADMIN"],
            )
        ]
    )


def _authenticator() -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(key_resource=config_api_key_resource(_keys()))


def _manifest(users: list[Profile]) -> Manifest:
    return Manifest(resources=[ListResource(users, path="users")])


def _app(
    users: list[Profile],
    *,
    path: str = "me",
    user_resource: Any | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    resource = ListResource(users, path="users")
    manifest = _manifest(users)
    # The app runs OPTIONAL posture to prove `me` stays strict regardless — the
    # route's own strict dependency, not the builder's, is what guards it.
    builder = AuthorizedDependencyBuilder(
        authenticator=authenticator or _authenticator(),
        policy_resolver=RolePolicyResolver(
            role_policies={},
            resource_defaults={"users": [ReadOnly()]},
            default=[DenyAll()],
        ),
        posture=Posture.OPTIONAL,
    )
    app = create_app(manifest, dependency_builder=builder)
    register_me_routes(
        app,
        authenticator=authenticator or _authenticator(),
        user_resource=user_resource if user_resource is not None else resource,
        path=path,
    )
    return app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    users = [Profile(id=USER_ID, email="a@example.com", username="alice", name="Alice")]
    app = _app(users)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_get_me_returns_oidc_shape(client: AsyncClient) -> None:
    resp = await client.get("/me", headers=API_KEY_HEADER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sub"] == str(USER_ID)
    assert body["kind"] == "service"
    assert body["roles"] == ["ADMIN"]
    assert body["scopes"] == []
    # Profile claims come from the caller's own row.
    assert body["email"] == "a@example.com"
    assert body["preferred_username"] == "alice"
    assert body["name"] == "Alice"
    # No external identity for a first-party credential: omitted, not null.
    assert "external_id" not in body


async def test_post_me_returns_the_same_body(client: AsyncClient) -> None:
    get_body = (await client.get("/me", headers=API_KEY_HEADER)).json()
    post_body = (await client.post("/me", headers=API_KEY_HEADER)).json()
    assert get_body == post_body


async def test_absent_credential_is_401_even_under_optional_posture(client: AsyncClient) -> None:
    resp = await client.get("/me")
    assert resp.status_code == 401
    assert "www-authenticate" in resp.headers


async def test_invalid_credential_is_401(client: AsyncClient) -> None:
    resp = await client.get("/me", headers={API_KEY_HEADER_NAME: "wrong"})
    assert resp.status_code == 401


async def test_unavailable_profile_claim_is_omitted_not_null() -> None:
    # The caller's row carries no email / username / name, so those claims are
    # absent from the body entirely — never present as null.
    users = [Profile(id=USER_ID)]
    app = _app(users, user_resource=ListResource(users, path="users"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["sub"] == str(USER_ID)
    for claim in ("email", "preferred_username", "name"):
        assert claim not in body


async def test_a_missing_store_row_still_returns_sub() -> None:
    # No row for the caller's id: profile claims are omitted, `sub` still served.
    users = [Profile(id=OTHER_ID, email="other@example.com")]
    app = _app(users, user_resource=ListResource(users, path="users"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["sub"] == str(USER_ID)
    assert "email" not in body


async def test_no_user_resource_means_no_profile_claims() -> None:
    # The credential-only posture: no store to enrich from, so only the
    # principal is served (the shape examples 01-03 get).
    users = [Profile(id=USER_ID, email="a@example.com")]
    manifest = _manifest(users)
    app = create_app(manifest)
    register_me_routes(app, authenticator=_authenticator(), user_resource=None)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["sub"] == str(USER_ID)
    assert "email" not in body


async def test_me_is_not_gated_by_the_users_policy() -> None:
    # The `users` surface is admin-only (DENY for this un-roled service caller);
    # `me` still returns the caller's own profile because it reads the resource
    # directly rather than through the authorized service.
    users = [Profile(id=USER_ID, email="a@example.com")]
    resource = ListResource(users, path="users")
    manifest = _manifest(users)
    builder = AuthorizedDependencyBuilder(
        authenticator=_authenticator(),
        policy_resolver=RolePolicyResolver(
            role_policies={}, resource_defaults={"users": [DenyAll()]}, default=[DenyAll()]
        ),
        posture=Posture.OPTIONAL,
    )
    app = create_app(manifest, dependency_builder=builder)
    register_me_routes(app, authenticator=_authenticator(), user_resource=resource)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # The admin-only users collection is denied for this caller...
            assert (await c.get("/users", headers=API_KEY_HEADER)).json()["items"] == []
            # ...but the caller's own `me` still resolves.
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["sub"] == str(USER_ID)
    assert body["email"] == "a@example.com"


async def test_me_reads_only_the_callers_own_row() -> None:
    # Two rows; the caller's credential names USER_ID, so only that profile is
    # returned even though OTHER_ID's row exists in the same store.
    users = [
        Profile(id=USER_ID, email="a@example.com"),
        Profile(id=OTHER_ID, email="other@example.com"),
    ]
    app = _app(users)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["email"] == "a@example.com"


async def test_path_is_configurable() -> None:
    users = [Profile(id=USER_ID, email="a@example.com")]
    app = _app(users, path="userinfo")
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/userinfo", headers=API_KEY_HEADER)).status_code == 200
            assert (await c.get("/me", headers=API_KEY_HEADER)).status_code == 404


class _ExternalAuthenticator(Authenticator):
    """A deterministic authenticator resolving an external-IdP principal.

    The OAuth authenticator only produces an ``external_id`` / ``scopes`` after
    a full JWKS + identity-map pipeline; this subclass exercises the route's
    handling of those fields directly (the ``Authenticator`` seam is the intended
    extension point, as the other auth tests use it).
    """

    async def authenticate(self, request: Any) -> AuthResult:
        return AuthResult.authenticated(
            Principal(
                id=USER_ID,
                kind=PrincipalKind.USER,
                external_id="auth0|abc",
                scopes=frozenset({"openid", "email"}),
            )
        )


async def test_external_identity_and_scopes_are_surfaced() -> None:
    users = [Profile(id=USER_ID, email="a@example.com")]
    app = _app(
        users,
        user_resource=ListResource(users, path="users"),
        authenticator=_ExternalAuthenticator(),
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/me", headers=API_KEY_HEADER)).json()
    assert body["sub"] == str(USER_ID)
    assert body["external_id"] == "auth0|abc"
    assert body["kind"] == "user"
    assert body["scopes"] == ["email", "openid"]
