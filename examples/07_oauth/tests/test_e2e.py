"""End-to-end tests for the OAuth example (SQLite).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The schema is created by applying the committed Alembic migration —
the same revision ``alembic upgrade head`` applies — so the migration (and the
seed it performs) is verified.

The app is assembled through the real config path and httpx's ASGI transport, so
requests drive the full request → OAuth verify → identity map → local user →
role → service → SQLAlchemy stack. The dev JWKS fetcher and the token-exchange
poster are injected, so no network is touched.

The board's authorization rules are the ones example 04 established: reads are
public, a ``USER`` may create a message and edit only its own, and the local user
store is authoritative (a disabled user is rejected however valid the token).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oauth_example.app import build_app
from oauth_example.dev_idp import DEV_AUDIENCE, DEV_ISSUER, make_dev_token
from oauth_example.models import Thread, User
from oauth_example.seed import ADMIN_SUBJECT, USER_ID, USER_SUBJECT
from resourcey.auth.auth_oauth_config import IdpConfig, OAuthClientConfig
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig


def _config() -> IdpConfig:
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


@pytest_asyncio.fixture
async def wired(
    tmp_path: Path, monkeypatch
) -> AsyncIterator[tuple[AsyncClient, SqlSessionManager]]:
    """A fully wired REST client backed by a migrated SQLite file.

    Yields the client and the app's session manager, so a test can mutate the
    local user store (e.g. disable a user) on the same database the app reads.
    """
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-07")
    monkeypatch.setenv("APP_SESSION_COOKIE_SECURE", "false")
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app, _setup = build_app(session_manager=manager, config=_config())
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, manager
    finally:
        await manifest.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(wired: tuple[AsyncClient, SqlSessionManager]) -> AsyncClient:
    return wired[0]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _set_user_enabled(manager: SqlSessionManager, user_id: object, *, enabled: bool) -> None:
    maker: async_sessionmaker[AsyncSession] = await manager.get_session_maker()
    async with maker() as session:
        await session.execute(update(User).where(User.id == user_id).values(enabled=enabled))
        await session.commit()


async def _make_thread(client: AsyncClient, token: str, title: str = "T") -> dict[str, object]:
    resp = await client.post("/threads", json={"title": title}, headers=_bearer(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_message(
    client: AsyncClient, token: str, thread_id: object, text: str
) -> dict[str, object]:
    resp = await client.post(
        "/messages", json={"thread_id": thread_id, "text": text}, headers=_bearer(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Posture: reads public, a bad credential still rejected
# ---------------------------------------------------------------------------


class TestPosture:
    async def test_anonymous_reads_are_allowed(self, client: AsyncClient) -> None:
        assert (await client.get("/threads")).status_code == 200
        assert (await client.get("/messages")).status_code == 200

    async def test_anonymous_writes_are_rejected(self, client: AsyncClient) -> None:
        assert (await client.post("/threads", json={"title": "N"})).status_code == 403

    async def test_a_bad_token_is_rejected_even_on_a_read(self, client: AsyncClient) -> None:
        assert (await client.get("/threads", headers=_bearer("nope"))).status_code == 401


# ---------------------------------------------------------------------------
# Inbound verification: a provider token authenticates
# ---------------------------------------------------------------------------


class TestInbound:
    async def test_a_valid_token_authenticates(self, client: AsyncClient) -> None:
        token = make_dev_token(subject=USER_SUBJECT)
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 200

    async def test_unknown_issuer_is_invalid(self, client: AsyncClient) -> None:
        token = make_dev_token(subject=USER_SUBJECT, issuer="https://evil.example")
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 401

    async def test_wrong_audience_is_invalid(self, client: AsyncClient) -> None:
        token = make_dev_token(subject=USER_SUBJECT, audience="someone-else")
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 401

    async def test_expired_token_is_invalid(self, client: AsyncClient) -> None:
        token = make_dev_token(subject=USER_SUBJECT, expires_in=-100)
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 401

    async def test_unmapped_subject_is_fail_closed(self, client: AsyncClient) -> None:
        token = make_dev_token(subject="who-is-this")
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 401


# ---------------------------------------------------------------------------
# The local user store is authoritative
# ---------------------------------------------------------------------------


class TestLocalStoreAuthoritative:
    async def test_disabling_a_local_user_revokes_the_token(
        self, wired: tuple[AsyncClient, SqlSessionManager]
    ) -> None:
        client, manager = wired
        token = make_dev_token(subject=USER_SUBJECT)
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 200
        await _set_user_enabled(manager, USER_ID, enabled=False)
        assert (await client.get("/threads", headers=_bearer(token))).status_code == 401


# ---------------------------------------------------------------------------
# Authorization: a USER creates and edits only its own rows
# ---------------------------------------------------------------------------


class TestUserOwnRows:
    async def test_a_user_creates_a_message(self, client: AsyncClient) -> None:
        admin = make_dev_token(subject=ADMIN_SUBJECT, roles=["ADMIN"])
        thread = await _make_thread(client, admin)
        user = make_dev_token(subject=USER_SUBJECT)
        message = await _make_message(client, user, thread["id"], "hi")
        # The author is stamped from the principal, not the client body.
        assert message["author_id"] == str(USER_ID)

    async def test_a_user_reads_every_message(self, client: AsyncClient) -> None:
        admin = make_dev_token(subject=ADMIN_SUBJECT, roles=["ADMIN"])
        thread = await _make_thread(client, admin)
        await _make_message(client, admin, thread["id"], "from admin")
        user = make_dev_token(subject=USER_SUBJECT)
        resp = await client.get("/messages", headers=_bearer(user))
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    async def test_a_user_edits_only_its_own_message(self, client: AsyncClient) -> None:
        admin = make_dev_token(subject=ADMIN_SUBJECT, roles=["ADMIN"])
        thread = await _make_thread(client, admin)
        user = make_dev_token(subject=USER_SUBJECT)
        own = await _make_message(client, user, thread["id"], "mine")
        other = await _make_message(client, admin, thread["id"], "theirs")

        # The user edits its own row.
        edited = await client.patch(
            f"/messages/{own['id']}", json={"text": "edited"}, headers=_bearer(user)
        )
        assert edited.status_code == 200, edited.text
        # ...but a row it does not own is a 404 (existence is not leaked).
        denied = await client.patch(
            f"/messages/{other['id']}", json={"text": "nope"}, headers=_bearer(user)
        )
        assert denied.status_code == 404


# ---------------------------------------------------------------------------
# The exposed client surface
# ---------------------------------------------------------------------------


class TestClientSurface:
    async def test_the_client_secret_is_never_served(self, client: AsyncClient) -> None:
        # The client surface is a privilege surface (admin-only by the fail-closed
        # default), so an admin token is required to read it at all.
        admin = make_dev_token(subject=ADMIN_SUBJECT, roles=["ADMIN"])
        resp = await client.get("/oauth-clients", headers=_bearer(admin))
        assert resp.status_code == 200
        assert resp.json()["items"]
        for item in resp.json()["items"]:
            assert "client_secret" not in item
            assert "issuer" in item

    async def test_the_client_surface_is_not_public(self, client: AsyncClient) -> None:
        # Anonymous / non-admin callers get an empty page, not the config.
        assert (await client.get("/oauth-clients")).json()["items"] == []
        user = make_dev_token(subject=USER_SUBJECT)
        assert (await client.get("/oauth-clients", headers=_bearer(user))).json()["items"] == []

    async def test_the_token_table_is_not_exposed(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert not any("oauth-tokens" in path for path in schema["paths"])


# ---------------------------------------------------------------------------
# The interactive flow (login -> callback -> session cookie)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


@pytest_asyncio.fixture
async def flow_client(tmp_path: Path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A client for the interactive flow, with the token exchange faked.

    The callback's code exchange is the one external call; it is injected, so the
    flow runs end to end without a network. The IdP "returns" a dev token for the
    seeded ``dev-user`` subject.
    """
    db_path = tmp_path / "flow.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_VALUE", "test-secret-key-for-example-07")
    monkeypatch.setenv("APP_SESSION_COOKIE_SECURE", "false")
    SqlConfig.clear_instance_cache()
    _apply_migration(async_url)

    idp_token = make_dev_token(subject=USER_SUBJECT)

    async def fake_post(
        url: str, *, data: dict[str, str], headers: dict[str, str]
    ) -> _FakeResponse:
        return _FakeResponse(
            {
                "access_token": idp_token,
                "refresh_token": "IDP-REFRESH",
                "expires_in": 120,
                "scope": "openid",
            }
        )

    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app, _setup = build_app(
        session_manager=manager, config=_config(), http_post=fake_post
    )
    await manifest.__aenter__()
    try:
        # A thread for the session-cookie write test (the USER role may create
        # messages but not threads).
        maker: async_sessionmaker[AsyncSession] = await manager.get_session_maker()
        async with maker() as session:
            session.add(Thread(title="seeded"))
            await session.commit()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


class TestInteractiveFlow:
    async def test_login_redirects_with_state_and_pkce(self, flow_client: AsyncClient) -> None:
        response = await flow_client.get("/oauth/login", params={"client": "dev"})
        assert response.status_code == 307
        location = response.headers["location"]
        assert location.startswith("https://dev-idp.example/authorize?")
        assert "code_challenge=" in location
        assert "code_challenge_method=S256" in location
        assert "state=" in location

    async def test_callback_mints_a_session_cookie_that_authenticates(
        self, flow_client: AsyncClient
    ) -> None:
        login = await flow_client.get("/oauth/login", params={"client": "dev"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        callback = await flow_client.get("/oauth/callback", params={"code": "abc", "state": state})
        assert callback.status_code == 200, callback.text
        # The browser now holds *our* session cookie, not the provider's token.
        assert "session" in callback.cookies

    async def test_the_session_cookie_authenticates_a_write(self, flow_client: AsyncClient) -> None:
        # The BFF point: after the flow, the browser presents our cookie and the
        # request is authenticated (the USER role grants the message write).
        login = await flow_client.get("/oauth/login", params={"client": "dev"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        await flow_client.get("/oauth/callback", params={"code": "abc", "state": state})
        # httpx's cookie jar carries the session cookie on subsequent requests.
        created = await flow_client.post(
            "/messages", json={"thread_id": 1, "text": "from the session"}
        )
        assert created.status_code == 201, created.text

    async def test_callback_rejects_a_state_mismatch(self, flow_client: AsyncClient) -> None:
        await flow_client.get("/oauth/login", params={"client": "dev"})
        response = await flow_client.get(
            "/oauth/callback", params={"code": "abc", "state": "wrong"}
        )
        assert response.status_code == 400
