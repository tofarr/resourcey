"""Smoke tests for the file-store example app (issues #117, #158).

Exercises the whole flow over HTTP -- upload (``POST`` a ``multipart/form-data``
body, ``201``: the file exists the instant this returns) -> read / download /
content -> delete -- against a :class:`LocalFileStore` rooted in ``tmp_path``.
There is no metadata table at all for this (default) medium, so no schema setup
is needed. The committed-migration e2e path (the optional SQL medium) lives in
``test_e2e.py``.

The app is assembled through the shipped :func:`file_store_example.app.build_app`,
so the tests exercise the real wiring (the medium in the manifest's ``managers``
slot, the ``files`` surface mounted after ``create_app``) rather than a
hand-injected dependency.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from file_store_example.app import build_app
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.local_file_store import LocalFileStore


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A file-store app over a tmp-dir local medium (no database needed)."""
    store = LocalFileStore(root=tmp_path / "blobs")
    manifest, app = build_app(store=store, config=FileStoreConfig())
    await manifest.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)


async def _create_file(
    client: AsyncClient,
    *,
    name: str = "notes.txt",
    content_type: str = "text/plain",
    body: bytes = b"hello",
) -> dict[str, object]:
    resp = await client.post("/files", files={"file": (name, body, content_type)})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_app_mounts_the_files_surface_and_local_transfer_route() -> None:
    """``build_app`` mounts the standard actions plus download / content."""
    manifest, app = build_app(store=LocalFileStore(root="/tmp/x"))
    assert manifest.resources == ()  # files is not a manifest resource (see app.py)

    paths = set(app.openapi()["paths"])
    assert {"/files", "/files/{id}", "/files/count", "/files/batch-read"} <= paths
    assert "/files/{id}/download" in paths
    assert "/files/{id}/content" in paths
    # /files's create route is a multipart/form-data body, not JSON.
    assert "multipart/form-data" in app.openapi()["paths"]["/files"]["post"]["requestBody"]["content"]
    # The framework-signed transfer route is served but hidden from the schema.
    assert "/_files/{key}" not in paths


def test_create_has_no_declared_size_or_checksum_input() -> None:
    """There is nothing left in the create request for a client to declare."""
    from file_store_example.files import build_files_resource

    resource = build_files_resource(LocalFileStore(root="/tmp/x"))
    models = resource.get_rest_models()
    assert set(models.create_request.model_fields) == set()
    assert {"name", "content_type", "size", "checksum", "etag", "updated_at"} <= set(
        models.read_response.model_fields
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_create_is_the_upload(client: AsyncClient) -> None:
    body = await _create_file(client)
    assert body["name"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["size"] == 5
    assert body["checksum"]
    # No separate transfer step -- the file already exists.
    read = await client.get(f"/files/{body['id']}")
    assert read.status_code == 200
    assert read.json()["etag"]


async def test_size_and_checksum_are_computed_not_declared(client: AsyncClient) -> None:
    created = await _create_file(client, body=b"hello world")
    assert created["size"] == len(b"hello world")
    assert created["checksum"] == hashlib.sha256(b"hello world").hexdigest()


async def test_happy_path(client: AsyncClient) -> None:
    created = await _create_file(client)
    file_id = created["id"]

    download = await client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    assert download.json()["method"] == "GET"
    fetched = await client.get(download.json()["url"])
    assert fetched.status_code == 200
    assert fetched.content == b"hello"
    assert fetched.headers["content-type"].startswith("text/plain")

    content = await client.get(f"/files/{file_id}/content")
    assert content.status_code == 200
    assert content.content == b"hello"
    assert content.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def test_download_and_content_for_a_missing_id_are_404(client: AsyncClient) -> None:
    assert (await client.get("/files/does-not-exist/download")).status_code == 404
    assert (await client.get("/files/does-not-exist/content")).status_code == 404


async def test_transfer_requires_a_token(client: AsyncClient) -> None:
    resp = await client.get("/_files/whatever")
    assert resp.status_code == 400


async def test_capability_is_bound_to_its_object(client: AsyncClient) -> None:
    created = await _create_file(client)
    download = await client.get(f"/files/{created['id']}/download")
    token = download.json()["url"].split("token=")[1]
    # Replaying the token against a different key is rejected.
    created2 = await _create_file(client, name="other.txt")
    wrong = await client.get(f"/_files/{created2['id']}?token={token}")
    assert wrong.status_code == 400


async def test_delete_removes_the_object(client: AsyncClient) -> None:
    created = await _create_file(client)

    deleted = await client.delete(f"/files/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/files/{created['id']}")).status_code == 404


async def test_batch_edit_cannot_create(client: AsyncClient) -> None:
    """A JSON batch body cannot carry a file upload -- only delete is offered."""
    resp = await client.post("/files/batch-edit", json=[{"kind": "Create", "item": {}}])
    assert resp.status_code == 422


async def test_batch_edit_can_delete(client: AsyncClient) -> None:
    created = await _create_file(client)
    resp = await client.post(
        "/files/batch-edit", json=[{"kind": "Delete", "id": created["id"]}]
    )
    assert resp.status_code == 200
    assert resp.json() == [None]
    assert (await client.get(f"/files/{created['id']}")).status_code == 404


async def test_max_size_rejects_an_oversize_upload(tmp_path: Path) -> None:
    store = LocalFileStore(root=tmp_path / "blobs")
    config = FileStoreConfig(max_size=4)
    manifest, app = build_app(store=store, config=config)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/files", files={"file": ("big.bin", b"hello world", "text/plain")})
            assert resp.status_code == 400
            ok = await c.post("/files", files={"file": ("ok.bin", b"ok", "text/plain")})
            assert ok.status_code == 201
