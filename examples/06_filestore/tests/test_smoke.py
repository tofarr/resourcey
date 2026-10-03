"""Smoke tests for the file-store example app (issue #158).

Exercises the whole flow over HTTP — allocate a key + mint an upload capability
(``202``) -> transfer the bytes against it -> the file now exists -> download /
content -> delete — against a :class:`LocalFileStore` rooted in ``tmp_path``.
There is no metadata table at all for this (default) medium, so no schema setup
is needed. The committed-migration e2e path (the optional SQL medium) lives in
``test_e2e.py``.

The app is assembled through the shipped :func:`file_store_example.app.build_app`,
so the tests exercise the real wiring (the medium in the manifest's ``managers``
slot, the ``files`` surface mounted after ``create_app``) rather than a
hand-injected dependency.
"""

from __future__ import annotations

import uuid
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
    client: AsyncClient, *, name: str = "notes.txt", content_type: str = "text/plain", size: int = 5
) -> dict[str, object]:
    resp = await client.post(
        "/files", json={"name": name, "content_type": content_type, "size": size}
    )
    assert resp.status_code == 202
    return resp.json()


async def _upload(client: AsyncClient, created: dict[str, object], body: bytes) -> None:
    """Transfer the bytes against the capability a ``create`` response minted."""
    cap = created["upload"]
    assert isinstance(cap, dict)
    put = await client.put(str(cap["url"]), content=body, headers={"content-type": "text/plain"})
    assert put.status_code == 200


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_app_mounts_the_files_surface_and_local_transfer_routes() -> None:
    """``build_app`` mounts the standard actions plus download / content."""
    manifest, app = build_app(store=LocalFileStore(root="/tmp/x"))
    assert manifest.resources == ()  # files is not a manifest resource (see app.py)

    paths = set(app.openapi()["paths"])
    assert {"/files", "/files/{id}", "/files/count", "/files/batch-read"} <= paths
    assert "/files/{id}/download" in paths
    assert "/files/{id}/content" in paths
    # The framework-signed transfer routes are served but hidden from the schema.
    assert "/_files/{key}" not in paths


def test_upload_field_only_appears_on_create() -> None:
    """The one-time upload capability never reaches read / search."""
    from file_store_example.files import build_files_resource

    resource = build_files_resource(LocalFileStore(root="/tmp/x"))
    models = resource.get_rest_models()
    assert "upload" in models.create_response.model_fields
    assert "upload" not in models.read_response.model_fields
    assert {"name", "content_type", "size", "etag", "updated_at"} <= set(
        models.read_response.model_fields
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_create_mints_an_upload_capability(client: AsyncClient) -> None:
    body = await _create_file(client)
    assert body["name"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["upload"]["method"] == "PUT"
    assert "token=" in body["upload"]["url"]


async def test_file_does_not_exist_until_uploaded(client: AsyncClient) -> None:
    created = await _create_file(client)
    file_id = created["id"]
    assert (await client.get(f"/files/{file_id}")).status_code == 404

    await _upload(client, created, b"hello")
    read = await client.get(f"/files/{file_id}")
    assert read.status_code == 200
    assert read.json()["content_type"] == "text/plain"
    assert read.json()["etag"]


async def test_happy_path(client: AsyncClient) -> None:
    created = await _create_file(client)
    file_id = created["id"]
    await _upload(client, created, b"hello")

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


async def test_download_and_content_before_upload_are_404(client: AsyncClient) -> None:
    created = await _create_file(client)
    assert (await client.get(f"/files/{created['id']}/download")).status_code == 404
    assert (await client.get(f"/files/{created['id']}/content")).status_code == 404


async def test_transfer_requires_a_token(client: AsyncClient) -> None:
    resp = await client.put("/_files/whatever")
    assert resp.status_code == 400


async def test_capability_is_bound_to_its_object(client: AsyncClient) -> None:
    created = await _create_file(client)
    token = created["upload"]["url"].split("token=")[1]
    # Replaying the token against a different key is rejected.
    wrong = await client.put(f"/_files/{uuid.uuid4().hex}?token={token}", content=b"x")
    assert wrong.status_code == 400


async def test_delete_removes_the_object(client: AsyncClient) -> None:
    created = await _create_file(client)
    await _upload(client, created, b"hello")

    deleted = await client.delete(f"/files/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/files/{created['id']}")).status_code == 404
