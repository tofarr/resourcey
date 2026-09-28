"""Smoke tests for the file-store example app.

Exercises the whole handshake over HTTP — create metadata → mint an upload URL →
transfer the bytes against it → complete → download — against an in-memory
SQLite schema built with ``create_all`` and a :class:`LocalFileStore` rooted in
``tmp_path``. The committed-migration e2e path lives in ``test_e2e.py``.

The app is assembled through the shipped :func:`file_store_example.app.build_app`,
so the tests exercise the real wiring (the medium in the manifest's ``managers``
slot, the handshake routes mounted after ``create_app``) rather than a
hand-injected dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from file_store_example.app import build_app
from file_store_example.models import Base
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.local_file_store import LocalFileStore
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig


async def _create_schema(maker: async_sessionmaker[AsyncSession]) -> None:
    """Create the metadata table over the manager's engine."""
    engine = maker.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A file-store app over in-memory SQLite and a tmp-dir local medium."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    store = LocalFileStore(root=tmp_path / "blobs")
    manifest, app = build_app(session_manager=manager, store=store, config=FileStoreConfig())
    await manifest.__aenter__()
    try:
        maker = await manager.get_session_maker()
        await _create_schema(maker)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


async def _create_file(
    client: AsyncClient, *, name: str = "notes.txt", content_type: str = "text/plain", size: int = 5
) -> dict[str, object]:
    resp = await client.post(
        "/files", json={"name": name, "content_type": content_type, "size": size}
    )
    assert resp.status_code == 201
    return resp.json()


async def _upload(
    client: AsyncClient, file_id: object, body: bytes, content_type: str = "text/plain"
):
    """Mint an upload URL, transfer the bytes, and complete the file."""
    upload = await client.post(f"/files/{file_id}/upload-url")
    assert upload.status_code == 200
    put = await client.put(
        upload.json()["url"], content=body, headers={"content-type": content_type}
    )
    assert put.status_code == 200
    return await client.post(f"/files/{file_id}/complete")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_app_mounts_the_handshake_and_local_transfer_routes() -> None:
    """``build_app`` registers the metadata resource and the file routes."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, app = build_app(session_manager=manager, store=LocalFileStore(root="/tmp/x"))

    assert {r.get_resource_path() for r in manifest.resources} == {"files"}
    paths = set(app.openapi()["paths"])
    assert {"/files", "/files/{id}", "/files/count", "/files/batch-read"} <= paths
    assert "/files/{id}/upload-url" in paths
    assert "/files/{id}/complete" in paths
    assert "/files/{id}/download" in paths
    # The framework-signed transfer routes are served but hidden from the schema.
    assert "/_files/{key}" not in paths


def test_storage_key_is_absent_from_the_read_model() -> None:
    """The storage ``key`` never reaches the read model (it is server-owned)."""
    manager = SqlSessionManager(SqlConfig.get_instance())
    manifest, _app = build_app(session_manager=manager, store=LocalFileStore(root="/tmp/x"))
    resource = manifest.resources[0]
    fields = set(resource.get_rest_models().read_response.model_fields)
    assert "key" not in fields
    assert {"name", "content_type", "size", "etag", "status", "updated_at"} <= fields


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_create_row_is_pending_and_assigns_a_hidden_key(client: AsyncClient) -> None:
    body = await _create_file(client)
    assert body["status"] == "pending"
    assert body["name"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert "key" not in body
    assert body["etag"] is None


async def test_happy_path_handshake(client: AsyncClient) -> None:
    created = await _create_file(client)
    file_id = created["id"]

    complete = await _upload(client, file_id, b"hello")
    assert complete.status_code == 200
    assert complete.json()["status"] == "ready"
    # MIME type + the medium's ETag flow back onto the row on completion.
    assert complete.json()["content_type"] == "text/plain"
    assert complete.json()["etag"]

    download = await client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    assert download.json()["method"] == "GET"
    fetched = await client.get(download.json()["url"])
    assert fetched.status_code == 200
    assert fetched.content == b"hello"
    assert fetched.headers["content-type"].startswith("text/plain")


async def test_upload_url_is_a_put_capability(client: AsyncClient) -> None:
    created = await _create_file(client)
    upload = await client.post(f"/files/{created['id']}/upload-url")
    assert upload.status_code == 200
    body = upload.json()
    assert body["method"] == "PUT"
    assert "token=" in body["url"]
    assert body["expires_at"]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def test_download_before_ready_is_conflict(client: AsyncClient) -> None:
    created = await _create_file(client)
    resp = await client.get(f"/files/{created['id']}/download")
    assert resp.status_code == 409


async def test_complete_without_upload_is_conflict(client: AsyncClient) -> None:
    created = await _create_file(client)
    resp = await client.post(f"/files/{created['id']}/complete")
    assert resp.status_code == 409


async def test_size_mismatch_fails_completion(client: AsyncClient) -> None:
    created = await _create_file(client, size=100)
    upload = await client.post(f"/files/{created['id']}/upload-url")
    await client.put(upload.json()["url"], content=b"short")
    resp = await client.post(f"/files/{created['id']}/complete")
    assert resp.status_code == 409


async def test_upload_url_for_absent_file_is_404(client: AsyncClient) -> None:
    import uuid

    resp = await client.post(f"/files/{uuid.uuid4()}/upload-url")
    assert resp.status_code == 404


async def test_transfer_requires_a_token(client: AsyncClient) -> None:
    resp = await client.put("/_files/whatever")
    assert resp.status_code == 400


async def test_capability_is_bound_to_its_object(client: AsyncClient) -> None:
    created = await _create_file(client)
    upload = await client.post(f"/files/{created['id']}/upload-url")
    token = upload.json()["url"].split("token=")[1]
    # Replaying the token against a different key is rejected.
    import uuid

    wrong = await client.put(f"/_files/{uuid.uuid4().hex}?token={token}", content=b"x")
    assert wrong.status_code == 400


async def test_delete_removes_the_object(client: AsyncClient) -> None:
    created = await _create_file(client)
    await _upload(client, created["id"], b"hello")

    deleted = await client.delete(f"/files/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/files/{created['id']}")).status_code == 404
