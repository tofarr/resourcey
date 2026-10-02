"""End-to-end tests for the file-store example's SQL medium (issue #158).

The default :class:`~resourcey.filestore.local_file_store.LocalFileStore`
medium needs no database, so the migration has nothing left to verify there;
this suite instead exercises the **SQL medium**
(:class:`~resourcey.filestore.sql_file_store.SqlFileStore`), applying the
committed Alembic migration to an isolated SQLite database file — the same
revision ``alembic upgrade head`` applies — so the migration itself is verified
rather than bypassed with ``create_all``.

The app is assembled through the real config path: the isolated URL is set as
``APP_SQL_CONNECTIONS_0_URL``, a fresh
:class:`~resourcey.sql.session_manager.SqlSessionManager` is built from
``SqlConfig.get_instance()``, and the ``store`` passed to
:func:`file_store_example.app.build_app` is a :class:`SqlFileStore` over that
same manager. Requests run through httpx's ASGI transport — the full request ->
router -> service -> SQLAlchemy stack.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient

from file_store_example.app import build_app
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.sql_file_store import SqlFileStore
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def _sync_url(async_url: str) -> str:
    """Alembic drives a sync engine; mirror ``migrations/env.py``'s conversion."""
    return async_url.replace("+aiosqlite", "")


def _apply_migration(async_url: str) -> None:
    """Apply the committed migration to the isolated database."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(_migrations_dir()))
    # env.py prefers an explicit sqlalchemy.url, so no APP_* env is needed here.
    config.set_main_option("sqlalchemy.url", _sync_url(async_url))
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A fully wired file-store client against a migrated SQLite file."""
    db_path = tmp_path / "e2e.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
    monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", async_url)
    SqlConfig.clear_instance_cache()

    _apply_migration(async_url)

    manager = SqlSessionManager(SqlConfig.get_instance())
    store = SqlFileStore(session_manager=manager, connection_name="main")
    manifest, app = build_app(session_manager=manager, store=store, config=FileStoreConfig())
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


async def _create(client: AsyncClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": "a.txt", "content_type": "text/plain", "size": 5}
    payload.update(overrides)
    resp = await client.post("/files", json=payload)
    assert resp.status_code == 202
    return resp.json()


async def _upload(client: AsyncClient, created: dict[str, object], body: bytes = b"hello") -> None:
    cap = created["upload"]
    assert isinstance(cap, dict)
    resp = await client.put(str(cap["url"]), content=body, headers={"content-type": "text/plain"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The ``files`` surface over the migrated SQL medium
# ---------------------------------------------------------------------------


class TestFilesSurface:
    async def test_create_mints_a_capability_and_persists_nothing(
        self, client: AsyncClient
    ) -> None:
        body = await _create(client, name="report.pdf", content_type="application/pdf", size=10)
        assert body["name"] == "report.pdf"
        assert body["content_type"] == "application/pdf"
        assert body["size"] == 10
        assert body["upload"]["method"] == "PUT"
        assert "id" in body
        assert (await client.get(f"/files/{body['id']}")).status_code == 404

    async def test_read_after_upload(self, client: AsyncClient) -> None:
        created = await _create(client)
        await _upload(client, created)
        resp = await client.get(f"/files/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == created["name"]
        assert resp.json()["etag"]

    async def test_read_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get(f"/files/{uuid.uuid4().hex}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_then_read_404(self, client: AsyncClient) -> None:
        created = await _create(client)
        await _upload(client, created)
        resp = await client.delete(f"/files/{created['id']}")
        assert resp.status_code == 204
        assert (await client.get(f"/files/{created['id']}")).status_code == 404

    async def test_search_and_count(self, client: AsyncClient) -> None:
        for name in ("one.txt", "two.txt", "three.txt"):
            created = await _create(client, name=name)
            await _upload(client, created)

        resp = await client.get("/files")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3
        assert (await client.get("/files/count")).json() == 3


# ---------------------------------------------------------------------------
# download / content
# ---------------------------------------------------------------------------


class TestDownloadAndContent:
    async def test_full_round_trip(self, client: AsyncClient) -> None:
        created = await _create(client, size=5)
        await _upload(client, created)

        download = (await client.get(f"/files/{created['id']}/download")).json()
        fetched = await client.get(download["url"])
        assert fetched.status_code == 200
        assert fetched.content == b"hello"

        content = await client.get(f"/files/{created['id']}/content")
        assert content.status_code == 200
        assert content.content == b"hello"

    async def test_download_and_content_require_an_upload(self, client: AsyncClient) -> None:
        created = await _create(client)
        assert (await client.get(f"/files/{created['id']}/download")).status_code == 404
        assert (await client.get(f"/files/{created['id']}/content")).status_code == 404

    async def test_expired_or_foreign_capability_is_rejected(self, client: AsyncClient) -> None:
        created = await _create(client)
        token = created["upload"]["url"].split("token=")[1]
        resp = await client.put(f"/_files/{uuid.uuid4().hex}?token={token}", content=b"x")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    async def test_read_is_cacheable(self, client: AsyncClient) -> None:
        created = await _create(client)
        await _upload(client, created)
        fetched = await client.get(f"/files/{created['id']}")
        last_modified = fetched.headers.get("last-modified")
        assert last_modified

        conditional = await client.get(
            f"/files/{created['id']}", headers={"if-modified-since": last_modified}
        )
        assert conditional.status_code == 304
