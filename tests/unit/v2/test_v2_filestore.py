"""Tests for the ``v2`` pre-signed-URL file store (issue #117).

The feature has three parts, exercised here against the **real** production code
path (no mocks except the S3 client, which stands in for a network the tests
cannot reach):

* the **medium seam** -- :class:`LocalFileStore` over ``tmp_path`` and
  :class:`SqlFileStore` over in-memory SQLite;
* the **framework-signed capability** -- mint / verify, expiry enforcement, and
  the ``(key, op)`` binding;
* the **metadata resource + handshake** -- ``updated_at`` / MIME type on the row,
  ETag caching, and create -> upload-url -> transfer -> complete -> download end
  to end.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.filestore.file_config import FileStoreConfig
from resourcey.v2.filestore.file_metadata import (
    PENDING,
    READY,
    FileMetadataBase,
    file_resource,
)
from resourcey.v2.filestore.file_routes import register_file_routes
from resourcey.v2.filestore.file_store import FileStore
from resourcey.v2.filestore.local_file_store import LocalFileStore
from resourcey.v2.filestore.s3_file_store import S3FileStore
from resourcey.v2.filestore.signed_url import mint_signed_url, verify_signed_url
from resourcey.v2.filestore.sql_file_store import SqlFileStore, create_blob_tables
from resourcey.v2.http.app import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-files")
        )
    )


# ---------------------------------------------------------------------------
# LocalFileStore (real filesystem, tmp_path)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def local_store(tmp_path: Any) -> AsyncIterator[LocalFileStore]:
    store = LocalFileStore(root=tmp_path / "blobs")
    async with store:
        yield store


class TestLocalFileStore:
    async def test_put_get_head_delete_round_trip(self, local_store: LocalFileStore) -> None:
        stored = await local_store.put("abc123", b"hello", content_type="text/plain")
        assert stored.size == 5
        assert stored.content_type == "text/plain"
        assert stored.updated_at is not None

        assert await local_store.get("abc123") == b"hello"

        head = await local_store.head("abc123")
        assert head is not None
        assert head.size == 5
        # The MIME type survives in the sidecar index file the local medium keeps.
        assert head.content_type == "text/plain"

        await local_store.delete("abc123")
        assert await local_store.get("abc123") is None
        assert await local_store.head("abc123") is None

    async def test_get_missing_is_none(self, local_store: LocalFileStore) -> None:
        assert await local_store.get("nope") is None
        assert await local_store.head("nope") is None

    async def test_delete_missing_is_a_noop(self, local_store: LocalFileStore) -> None:
        await local_store.delete("nope")

    async def test_rejects_path_traversal(self, local_store: LocalFileStore) -> None:
        for key in ("../escape", "a/../../b", "/etc/passwd", "~/.ssh/id_rsa", ""):
            with pytest.raises(InvalidInputError):
                await local_store.put(key, b"x")

    async def test_put_creates_parent_directories(self, local_store: LocalFileStore) -> None:
        await local_store.put("tenant/sub/key", b"x")
        assert await local_store.get("tenant/sub/key") == b"x"

    async def test_corrupt_sidecar_is_ignored(self, local_store: LocalFileStore) -> None:
        await local_store.put("k", b"x", content_type="text/plain")
        meta = local_store._meta_path_for(local_store.root / "k")
        meta.write_text("not json", encoding="utf-8")
        head = await local_store.head("k")
        assert head is not None and head.content_type is None

    async def test_delete_removes_the_sidecar(self, local_store: LocalFileStore) -> None:
        await local_store.put("k", b"x", content_type="text/plain")
        meta = local_store._meta_path_for(local_store.root / "k")
        assert meta.is_file()
        await local_store.delete("k")
        assert not meta.is_file()

    async def test_presign_uses_framework_signing(self, local_store: LocalFileStore) -> None:
        url = local_store.presign_put("abc123", content_type="text/plain", expires_in_seconds=60)
        assert url.method == "PUT"
        assert "/_files/abc123" in url.url
        assert "token=" in url.url
        key = local_store.verify(url.url.split("token=")[1], expected_operation="put")
        assert key == "abc123"


# ---------------------------------------------------------------------------
# SqlFileStore (real SQLite, blob table)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sql_store() -> AsyncIterator[SqlFileStore]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await create_blob_tables(maker)
    store = SqlFileStore(session_factory=maker)
    async with store:
        yield store
    await engine.dispose()


class TestSqlFileStore:
    async def test_put_get_head_delete_round_trip(self, sql_store: SqlFileStore) -> None:
        stored = await sql_store.put("k1", b"data", content_type="application/json")
        assert stored.size == 4
        assert await sql_store.get("k1") == b"data"
        head = await sql_store.head("k1")
        assert head is not None and head.content_type == "application/json"
        await sql_store.delete("k1")
        assert await sql_store.get("k1") is None

    async def test_put_overwrites(self, sql_store: SqlFileStore) -> None:
        await sql_store.put("k1", b"one")
        await sql_store.put("k1", b"two")
        assert await sql_store.get("k1") == b"two"

    async def test_used_before_entry_raises(self) -> None:
        store = SqlFileStore(session_factory=None)
        with pytest.raises(Exception, match="before entering"):
            await store.get("k")

    async def test_missing_is_none(self, sql_store: SqlFileStore) -> None:
        assert await sql_store.get("absent") is None
        assert await sql_store.head("absent") is None

    async def test_entry_resolves_a_session_maker_from_a_manager(self) -> None:
        from resourcey.v2.sql.db_config import DbConfig
        from resourcey.v2.sql.session_manager import SqlSessionManager
        from resourcey.v2.sql.sql_config import SqlConfig

        manager = SqlSessionManager(
            SqlConfig(sql_connections=[DbConfig(name="main", url="sqlite+aiosqlite:///:memory:")])
        )
        store = SqlFileStore(session_manager=manager, connection_name="main")
        async with manager, store:
            await create_blob_tables(store.session_factory)  # type: ignore[arg-type]
            await store.put("k", b"x")
            assert await store.get("k") == b"x"

    def test_session_factory_is_none_before_entry(self) -> None:
        assert SqlFileStore(session_factory=None).session_factory is None


# ---------------------------------------------------------------------------
# S3FileStore (stub client; no network)
# ---------------------------------------------------------------------------


class _StubS3Client:
    """A minimal in-memory stand-in for a boto3 S3 client."""

    class exceptions:  # noqa: N801 - mirrors boto3's namespace
        class NoSuchKey(Exception): ...  # noqa: N818 - mirrors boto3

        class ClientError(Exception): ...

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kw: Any) -> None:  # noqa: N803
        self.objects[Key] = (Body, kw.get("ContentType"))

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        import io

        return {"Body": io.BytesIO(self.objects[Key][0])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.ClientError(Key)
        body, content_type = self.objects[Key]
        return {"ContentLength": len(body), "ContentType": content_type, "ETag": '"stub"'}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def generate_presigned_url(
        self,
        action: str,
        *,
        Params: dict[str, Any],  # noqa: N803 - mirrors boto3's keyword
        ExpiresIn: int,  # noqa: N803
    ) -> str:
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?action={action}"


@pytest_asyncio.fixture
async def s3_store() -> AsyncIterator[S3FileStore]:
    store = S3FileStore(bucket="bucket", prefix="files", client=_StubS3Client())
    async with store:
        yield store


class TestS3FileStore:
    async def test_round_trip_with_prefix(self, s3_store: S3FileStore) -> None:
        await s3_store.put("k1", b"abc", content_type="text/plain")
        assert await s3_store.get("k1") == b"abc"
        head = await s3_store.head("k1")
        assert head is not None and head.etag == '"stub"'
        await s3_store.delete("k1")
        assert await s3_store.get("k1") is None

    async def test_native_presign(self, s3_store: S3FileStore) -> None:
        url = s3_store.presign_put("k1", content_type="text/plain", expires_in_seconds=30)
        assert url.method == "PUT"
        assert url.headers["Content-Type"] == "text/plain"
        get = s3_store.presign_get("k1", expires_in_seconds=30)
        assert get.method == "GET"

    async def test_import_safe_without_boto3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Building a real client (no stub) without boto3 raises the actionable extra.
        # Force the absence so the test is independent of whether the dev env has it.
        monkeypatch.setitem(sys.modules, "boto3", None)
        store = S3FileStore(bucket="bucket")
        with pytest.raises(ImportError, match=r"resourcey\[s3\]"):
            store._get_client()

    async def test_no_prefix_uses_the_key_directly(self) -> None:
        store = S3FileStore(bucket="b", client=_StubS3Client())
        await store.put("k", b"x")
        assert store._object_key("k") == "k"

    async def test_put_without_content_type_omits_it(self, s3_store: S3FileStore) -> None:
        stored = await s3_store.put("k", b"x")
        assert stored.content_type is None

    async def test_head_missing_is_none(self, s3_store: S3FileStore) -> None:
        assert await s3_store.head("absent") is None

    async def test_size_cap_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from resourcey.v2.core.errors import ResourceyConfigError

        monkeypatch.setattr("resourcey.v2.filestore.s3_file_store.S3_MAX_PUT_BYTES", 4)
        store = S3FileStore(bucket="b", client=_StubS3Client())
        with pytest.raises(ResourceyConfigError, match="single-PUT cap"):
            await store.put("k", b"12345")

    async def test_client_property_exposes_the_stub(self, s3_store: S3FileStore) -> None:
        assert s3_store.client is not None

    def test_build_client_passes_connection_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _FakeBoto3:
            @staticmethod
            def client(name: str, **kwargs: Any) -> str:
                captured[name] = kwargs
                return "client"

        monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
        store = S3FileStore(
            bucket="b",
            region="us-east-1",
            endpoint_url="http://minio",
            access_key_id="id",
            secret_access_key="secret",
        )
        assert store._get_client() == "client"
        assert captured["s3"] == {
            "region_name": "us-east-1",
            "endpoint_url": "http://minio",
            "aws_access_key_id": "id",
            "aws_secret_access_key": "secret",
        }


# ---------------------------------------------------------------------------
# Signed URLs: expiry + (key, op) binding
# ---------------------------------------------------------------------------


class TestSignedUrl:
    def test_round_trip(self) -> None:
        url = mint_signed_url(_encryption(), "key-1", "put", expires_in_seconds=60)
        token = url.url.split("token=")[1]
        assert verify_signed_url(_encryption(), token, expected_operation="put") == "key-1"

    def test_wrong_operation_is_rejected(self) -> None:
        url = mint_signed_url(_encryption(), "key-1", "put", expires_in_seconds=60)
        token = url.url.split("token=")[1]
        with pytest.raises(InvalidInputError):
            verify_signed_url(_encryption(), token, expected_operation="get")

    def test_expiry_is_enforced_by_the_verifier(self) -> None:
        url = mint_signed_url(_encryption(), "key-1", "get", expires_in_seconds=-1)
        token = url.url.split("token=")[1]
        with pytest.raises(InvalidInputError, match="expired"):
            verify_signed_url(_encryption(), token, expected_operation="get")

    def test_tampered_token_is_rejected(self) -> None:
        with pytest.raises(InvalidInputError):
            verify_signed_url(_encryption(), "not-a-token", expected_operation="get")

    def test_unknown_operation_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown signed-URL operation"):
            mint_signed_url(_encryption(), "k", "patch", expires_in_seconds=60)

    def test_token_without_key_claim_is_rejected(self) -> None:
        service = _encryption()
        token = service.create_jwe_token({"op": "get"}, expires_in=timedelta(seconds=60))
        with pytest.raises(InvalidInputError, match="key claim"):
            verify_signed_url(service, token, expected_operation="get")

    def test_token_without_expiry_is_rejected(self) -> None:
        service = _encryption()
        token = service.create_jwe_token({"k": "k", "op": "get"}, expires_in=timedelta(seconds=60))
        # Strip the expiry to exercise the "no expiry" guard.
        from resourcey.v2.filestore import signed_url as module

        original = service.decrypt_jwe_token

        def _no_exp(t: str) -> dict[str, object]:
            claims = dict(original(t))
            claims.pop("exp", None)
            return claims

        service.decrypt_jwe_token = _no_exp  # type: ignore[method-assign]
        try:
            with pytest.raises(InvalidInputError, match="no expiry"):
                module.verify_signed_url(service, token, expected_operation="get")
        finally:
            service.decrypt_jwe_token = original  # type: ignore[method-assign]

    def test_verify_binds_the_key(self) -> None:
        store = LocalFileStore()
        url = store.presign_put("k-1", content_type=None, expires_in_seconds=60)
        token = url.url.split("token=")[1]
        assert store.verify(token, expected_operation="put") == "k-1"


# ---------------------------------------------------------------------------
# Metadata resource: updated_at, MIME type, ETag
# ---------------------------------------------------------------------------


class TestFileMetadataResource:
    def test_updated_at_and_content_type_are_in_the_read_model(self) -> None:
        resource = file_resource(LocalFileStore())
        read = resource.get_rest_models().read_response
        assert "updated_at" in read.model_fields
        assert "created_at" in read.model_fields
        assert "content_type" in read.model_fields

    def test_key_and_status_are_not_client_writable(self) -> None:
        resource = file_resource(LocalFileStore())
        create = resource.get_rest_models().create_request.model_fields
        assert "key" not in create
        assert "status" not in create
        assert "content_type" in create

    def test_cache_strategy_is_etag(self) -> None:
        from resourcey.v2.cache.cache_strategy import ETagCacheStrategy

        resource = file_resource(LocalFileStore())
        assert isinstance(resource.get_cache_strategy(), ETagCacheStrategy)

    async def test_max_size_cap_refuses_an_oversize_declaration(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(FileMetadataBase.metadata.create_all)
        store = LocalFileStore()
        resource = file_resource(store, session_factory=maker, max_size=4)
        manifest = Manifest(resources=[resource], managers=[store])
        try:
            async with manifest:
                service = await resource.get_service({})
                async with service:
                    with pytest.raises(InvalidInputError, match="exceeds the 4-byte cap"):
                        await service.create(
                            resource.get_dto_type()(
                                name="big.bin", content_type="application/octet-stream", size=5
                            )
                        )
        finally:
            await engine.dispose()

    async def test_delete_removes_the_stored_object(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(FileMetadataBase.metadata.create_all)
        store = LocalFileStore()
        resource = file_resource(store, session_factory=maker)
        manifest = Manifest(resources=[resource], managers=[store])
        try:
            async with manifest:
                service = await resource.get_service({})
                async with service:
                    created = await service.create(
                        resource.get_dto_type()(name="a.txt", content_type="text/plain", size=5)
                    )
                    assert isinstance(created.key, str)
                    await store.put(created.key, b"hello", content_type="text/plain")
                    await service.delete(created.id)
                    assert await store.get(created.key) is None
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# End-to-end handshake over HTTP
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path: Any) -> AsyncIterator[AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FileMetadataBase.metadata.create_all)

    store = LocalFileStore(root=tmp_path / "blobs")
    resource = file_resource(store, session_factory=maker)
    manifest = Manifest(resources=[resource], managers=[store])
    app = create_app(manifest)
    register_file_routes(app, store, resource=resource, config=FileStoreConfig())

    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    await engine.dispose()


async def test_happy_path_handshake(client: AsyncClient) -> None:
    created = await client.post(
        "/files",
        json={"name": "notes.txt", "content_type": "text/plain", "size": 5},
    )
    assert created.status_code == 201
    body = created.json()
    file_id = body["id"]
    assert body["status"] == PENDING
    assert body["content_type"] == "text/plain"
    assert body["updated_at"] is not None
    assert "key" not in body

    upload = await client.post(f"/files/{file_id}/upload-url")
    assert upload.status_code == 200
    url = upload.json()["url"]
    assert upload.json()["method"] == "PUT"

    # The client transfers the bytes directly against the capability URL.
    put = await client.put(url, content=b"hello", headers={"content-type": "text/plain"})
    assert put.status_code == 200

    complete = await client.post(f"/files/{file_id}/complete")
    assert complete.status_code == 200
    assert complete.json()["status"] == READY
    # MIME type + ETag flow back onto the metadata row.
    assert complete.json()["content_type"] == "text/plain"
    assert complete.json()["etag"]

    download = await client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    get_url = download.json()["url"]
    fetched = await client.get(get_url)
    assert fetched.status_code == 200
    assert fetched.content == b"hello"
    assert fetched.headers["content-type"].startswith("text/plain")


async def test_read_is_etag_cacheable(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 0}
    )
    file_id = created.json()["id"]
    fetched = await client.get(f"/files/{file_id}")
    assert fetched.status_code == 200
    etag = fetched.headers.get("etag")
    assert etag

    conditional = await client.get(f"/files/{file_id}", headers={"if-none-match": etag})
    assert conditional.status_code == 304


async def test_download_before_ready_is_refused(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 5}
    )
    file_id = created.json()["id"]
    response = await client.get(f"/files/{file_id}/download")
    assert response.status_code == 409


async def test_complete_without_upload_fails(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 5}
    )
    file_id = created.json()["id"]
    response = await client.post(f"/files/{file_id}/complete")
    assert response.status_code == 409


async def test_size_mismatch_fails_completion(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 100}
    )
    file_id = created.json()["id"]
    upload = await client.post(f"/files/{file_id}/upload-url")
    await client.put(upload.json()["url"], content=b"short")
    response = await client.post(f"/files/{file_id}/complete")
    assert response.status_code == 409


async def test_delete_removes_the_stored_object(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 5}
    )
    file_id = created.json()["id"]
    upload = await client.post(f"/files/{file_id}/upload-url")
    await client.put(upload.json()["url"], content=b"hello")
    await client.post(f"/files/{file_id}/complete")

    deleted = await client.delete(f"/files/{file_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/files/{file_id}")).status_code == 404


async def test_signed_url_is_bound_to_its_object(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 5}
    )
    file_id = created.json()["id"]
    upload = await client.post(f"/files/{file_id}/upload-url")
    token = upload.json()["url"].split("token=")[1]
    # Replay the capability against a different opaque key: rejected.
    wrong = await client.put(f"/_files/{uuid4().hex}?token={token}", content=b"x")
    assert wrong.status_code == 400


async def test_missing_token_on_transfer_is_rejected(client: AsyncClient) -> None:
    response = await client.put(f"/_files/{uuid4().hex}")
    assert response.status_code == 400


async def test_signed_get_of_an_absent_object_is_404(client: AsyncClient) -> None:
    from resourcey.v2.encryption.encryption_service import get_encryption_service
    from resourcey.v2.filestore.signed_url import mint_signed_url

    token = mint_signed_url(
        get_encryption_service(), "ghost", "get", expires_in_seconds=60
    ).url.split("token=")[1]
    response = await client.get(f"/_files/ghost?token={token}")
    assert response.status_code == 404


async def test_completing_twice_is_refused(client: AsyncClient) -> None:
    created = await client.post(
        "/files", json={"name": "a.txt", "content_type": "text/plain", "size": 5}
    )
    file_id = created.json()["id"]
    upload = await client.post(f"/files/{file_id}/upload-url")
    await client.put(upload.json()["url"], content=b"hello")
    assert (await client.post(f"/files/{file_id}/complete")).status_code == 200
    assert (await client.post(f"/files/{file_id}/complete")).status_code == 409


async def test_upload_url_for_absent_file_is_404(client: AsyncClient) -> None:
    response = await client.post(f"/files/{uuid4()}/upload-url")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# FileStore typing / config
# ---------------------------------------------------------------------------


class TestFileStoreConfig:
    def test_defaults_and_ttl_properties(self) -> None:
        config = FileStoreConfig()
        assert config.upload_url_ttl == timedelta(seconds=config.upload_url_ttl_seconds)
        assert config.download_url_ttl_seconds > 0
        assert config.max_size is None

    def test_default_medium_is_local(self) -> None:
        assert isinstance(FileStoreConfig().medium, FileStore)
