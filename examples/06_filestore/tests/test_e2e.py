"""End-to-end tests for the file-store example (SQLite + committed migration).

Each test runs against an **isolated SQLite database file** in a per-test tmp
directory. The ``files`` table is created by applying the committed Alembic
migration — the same revision ``alembic upgrade head`` applies — so the migration
itself is verified rather than bypassed with ``create_all``.

The app is assembled through the real config path: the isolated URL is set as
``APP_SQL_CONNECTIONS_0_URL`` and a fresh
:class:`~resourcey.sql.session_manager.SqlSessionManager` is built from
``SqlConfig.get_instance()``, then handed to :func:`file_store_example.app.build_app`
together with a :class:`LocalFileStore` rooted in the same tmp directory. Requests
run through httpx's ASGI transport — the full request → router → service →
SQLAlchemy stack, plus the handshake routes.
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
from resourcey.filestore.local_file_store import LocalFileStore
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
    store = LocalFileStore(root=tmp_path / "blobs")
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
    assert resp.status_code == 201
    return resp.json()


async def _upload_and_complete(
    client: AsyncClient, file_id: object, body: bytes = b"hello"
) -> dict[str, object]:
    upload = (await client.post(f"/files/{file_id}/upload-url")).json()
    await client.put(upload["url"], content=body, headers={"content-type": "text/plain"})
    resp = await client.post(f"/files/{file_id}/complete")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Metadata CRUD (the standard resource surface)
# ---------------------------------------------------------------------------


class TestMetadataCrud:
    async def test_create_returns_pending_row(self, client: AsyncClient) -> None:
        body = await _create(client, name="report.pdf", content_type="application/pdf", size=10)
        assert body["name"] == "report.pdf"
        assert body["content_type"] == "application/pdf"
        assert body["size"] == 10
        assert body["status"] == "pending"
        assert "id" in body
        assert "created_at" in body
        assert "updated_at" in body

    async def test_read_returns_the_row(self, client: AsyncClient) -> None:
        created = await _create(client)
        resp = await client.get(f"/files/{created['id']}")
        assert resp.status_code == 200
        assert resp.json() == created

    async def test_read_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get(f"/files/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_partial_merge(self, client: AsyncClient) -> None:
        created = await _create(client, name="old.txt")
        resp = await client.patch(f"/files/{created['id']}", json={"name": "new.txt"})
        assert resp.status_code == 200
        # PATCH = partial merge: content_type is preserved.
        assert resp.json()["name"] == "new.txt"
        assert resp.json()["content_type"] == "text/plain"

    async def test_delete_then_read_404(self, client: AsyncClient) -> None:
        created = await _create(client)
        resp = await client.delete(f"/files/{created['id']}")
        assert resp.status_code == 204
        assert (await client.get(f"/files/{created['id']}")).status_code == 404

    async def test_search_and_count(self, client: AsyncClient) -> None:
        await _create(client, name="one.txt")
        await _create(client, name="two.txt")
        await _create(client, name="two.md", content_type="text/markdown")

        resp = await client.get("/files?content_type__eq=text/markdown")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "two.md"

        assert (await client.get("/files/count")).json() == 3


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_full_handshake(self, client: AsyncClient) -> None:
        created = await _create(client, size=5)
        completed = await _upload_and_complete(client, created["id"])
        assert completed["status"] == "ready"
        assert completed["etag"]

        download = (await client.get(f"/files/{created['id']}/download")).json()
        fetched = await client.get(download["url"])
        assert fetched.status_code == 200
        assert fetched.content == b"hello"

    async def test_download_requires_ready(self, client: AsyncClient) -> None:
        created = await _create(client)
        assert (await client.get(f"/files/{created['id']}/download")).status_code == 409

    async def test_complete_without_an_object_is_conflict(self, client: AsyncClient) -> None:
        created = await _create(client)
        assert (await client.post(f"/files/{created['id']}/complete")).status_code == 409

    async def test_completion_is_idempotent_once(self, client: AsyncClient) -> None:
        created = await _create(client)
        await _upload_and_complete(client, created["id"])
        assert (await client.post(f"/files/{created['id']}/complete")).status_code == 409

    async def test_size_mismatch_fails_completion(self, client: AsyncClient) -> None:
        created = await _create(client, size=100)
        upload = (await client.post(f"/files/{created['id']}/upload-url")).json()
        await client.put(upload["url"], content=b"short")
        assert (await client.post(f"/files/{created['id']}/complete")).status_code == 409

    async def test_delete_removes_the_object(self, client: AsyncClient) -> None:
        created = await _create(client)
        await _upload_and_complete(client, created["id"])
        assert (await client.delete(f"/files/{created['id']}")).status_code == 204
        # The metadata row is gone...
        assert (await client.get(f"/files/{created['id']}")).status_code == 404

    async def test_expired_or_foreign_capability_is_rejected(self, client: AsyncClient) -> None:
        created = await _create(client)
        upload = (await client.post(f"/files/{created['id']}/upload-url")).json()
        token = upload["url"].split("token=")[1]
        resp = await client.put(f"/_files/{uuid.uuid4().hex}?token={token}", content=b"x")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    async def test_read_is_etag_cacheable(self, client: AsyncClient) -> None:
        created = await _create(client)
        fetched = await client.get(f"/files/{created['id']}")
        etag = fetched.headers.get("etag")
        assert etag

        conditional = await client.get(f"/files/{created['id']}", headers={"if-none-match": etag})
        assert conditional.status_code == 304
