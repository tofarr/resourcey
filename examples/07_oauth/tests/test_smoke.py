"""Smoke tests for the OAuth example app.

These pin the app's factory wiring and the OAuth inbound path against an
in-memory SQLite database via httpx's ASGI transport, with the dev JWKS fetcher
injected so no network is touched. The end-to-end behaviour (the interactive
flow, migration-driven schema) lives in ``test_e2e.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from oauth_example.app import build_app
from oauth_example.dev_idp import DEV_AUDIENCE, DEV_ISSUER, make_dev_token
from oauth_example.models import Base, User
from oauth_example.seed import ADMIN_SUBJECT, USER_ID, USER_SUBJECT, seed
from resourcey.auth.auth_oauth import OAuthAuthenticator
from resourcey.auth.auth_oauth_config import IdpConfig, OAuthClientConfig

DISABLED_ID = USER_ID  # the id toggled off in the store-authoritative test


def _config() -> IdpConfig:
    """The dev client, as the committed ``.env`` supplies it."""
    return IdpConfig(
        oauth_clients=[
            OAuthClientConfig(
                id="dev",
                provider="dev",
                issuer=DEV_ISSUER,
                jwks_uri="https://dev-idp.example/jwks",
                audience=DEV_AUDIENCE,
                algorithms=["RS256"],
                client_id=DEV_AUDIENCE,
                client_secret=SecretStr("dev-client-secret"),
                auth_url="https://dev-idp.example/authorize",
                token_url="https://dev-idp.example/token",
                refresh_url="https://dev-idp.example/token",
                redirect_uri="http://test/oauth/callback",
                scopes=["openid", "email"],
                roles=["USER"],
            )
        ]
    )


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    new_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed(new_maker)
    try:
        yield new_maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[FastAPI]:
    manifest, built, _setup = build_app(config=_config(), session_factory=maker)
    await manifest.__aenter__()
    try:
        yield built
    finally:
        await manifest.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_build_app_wires_the_oauth_authenticator(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The factory always wires the OAuth authenticator and the role resolver."""
    _manifest, _app, setup = build_app(config=_config(), session_factory=maker)
    assert isinstance(setup.authenticator, OAuthAuthenticator)
    # The authenticator holds the local user store it validates resolved ids against.
    assert setup.authenticator.user_resource is not None
    # Reads are public; a presented-but-invalid token is still rejected.
    assert setup.authenticator.client_resource is not None


async def test_anonymous_read_is_allowed(client: AsyncClient) -> None:
    # Reads are public reference data, so no credential is fine (anonymous).
    assert (await client.get("/threads")).status_code == 200
    assert (await client.get("/messages")).status_code == 200


async def test_anonymous_write_is_rejected(client: AsyncClient) -> None:
    # Writes need a role grant, so an anonymous caller is denied (403).
    resp = await client.post("/threads", json={"title": "N"})
    assert resp.status_code == 403


async def test_a_bad_token_is_rejected_even_on_a_read(client: AsyncClient) -> None:
    # An absent credential is anonymous, but a *presented* bad one is a 401.
    assert (await client.get("/threads", headers=_bearer("not-a-jwt"))).status_code == 401


async def test_a_valid_dev_token_authenticates(client: AsyncClient) -> None:
    # The headline: a provider-issued token is verified, mapped to the local user,
    # and the request proceeds.
    token = make_dev_token(subject=USER_SUBJECT)
    assert (await client.get("/threads", headers=_bearer(token))).status_code == 200


async def test_an_unmapped_subject_is_fail_closed(client: AsyncClient) -> None:
    # A valid token for a subject with no ``ExternalIdentity`` link is rejected.
    token = make_dev_token(subject="who-is-this")
    assert (await client.get("/threads", headers=_bearer(token))).status_code == 401


async def test_the_local_user_store_is_authoritative(
    client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    # A valid provider token whose local user is disabled is rejected.
    token = make_dev_token(subject=USER_SUBJECT)
    assert (await client.get("/threads", headers=_bearer(token))).status_code == 200
    async with maker() as session:
        await session.execute(update(User).where(User.id == DISABLED_ID).values(enabled=False))
        await session.commit()
    assert (await client.get("/threads", headers=_bearer(token))).status_code == 401


async def test_client_secret_is_not_leaked(client: AsyncClient) -> None:
    # The client surface is admin-only (a privilege surface); read it as admin.
    admin = make_dev_token(subject=ADMIN_SUBJECT, roles=["ADMIN"])
    resp = await client.get("/oauth-clients", headers=_bearer(admin))
    assert resp.status_code == 200
    assert resp.json()["items"]
    for item in resp.json()["items"]:
        assert "client_secret" not in item
        # The verification fields stay readable.
        assert "issuer" in item


async def test_the_served_surfaces_are_narrowed(client: AsyncClient) -> None:
    """The client surface is read-only and the token table is not exposed."""
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert set(paths["/oauth-clients"]) == {"get"}
    assert set(paths["/users"]) == {"get"}
    assert set(paths["/users/{id}"]) == {"get"}
    # Debug-only token resource is deliberately absent from the manifest.
    assert not any("oauth-tokens" in path for path in paths)


async def test_me_returns_the_authenticated_principal(client: AsyncClient) -> None:
    """`GET /me` answers "who am I?" in the OIDC UserInfo shape (issue #150).

    The provider bearer token resolves to the seeded local user, so `sub` is the
    internal id (not the provider subject) and the local row enriches the body
    with profile claims.
    """
    token = make_dev_token(subject=USER_SUBJECT)
    resp = await client.get("/me", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sub"] == str(USER_ID)
    assert body["external_id"] == USER_SUBJECT
    assert body["kind"] == "user"
    # The roles come off the client row (APP_OAUTH_CLIENTS_0_ROLES).
    assert body["roles"] == ["USER"]
    # The caller's own row supplies the profile claims.
    assert body["email"] == "user@example.com"
    assert body["preferred_username"] == "user"


async def test_me_requires_a_credential(client: AsyncClient) -> None:
    """`me` has no anonymous meaning — an absent credential is a 401 even though
    reads of the board are public under the OPTIONAL posture, and an invalid one
    is a 401 too."""
    assert (await client.get("/me")).status_code == 401
    assert (await client.get("/me", headers=_bearer("not-a-jwt"))).status_code == 401
