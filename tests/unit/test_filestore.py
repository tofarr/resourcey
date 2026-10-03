"""Tests for the direct-upload file store (issues #117, #158).

The feature has four parts, exercised here against the **real** production code
path (no mocks except the S3 client, which stands in for a network the tests
cannot reach):

* the **medium seam** -- :class:`LocalFileStore` over ``tmp_path``,
  :class:`SqlFileStore` over in-memory SQLite, :class:`S3FileStore` over a stub
  client -- ``put`` / ``get`` / ``head`` / ``delete`` / ``list_objects`` /
  ``count_objects``;
* the **framework-signed download capability** -- mint / verify, expiry
  enforcement, and the key binding;
* the **``files`` resource** -- a medium-native existence record (no metadata
  table), its DTO / cache-strategy / query-surface shape, and the action layer;
* the **routes**, end to end over HTTP -- ``create`` (a ``multipart/form-data``
  upload, ``201``) -> ``read`` / ``search`` / ``count`` -> ``download``
  (capability) / ``content`` (bytes) -> ``delete``.
"""

from __future__ import annotations

import hashlib
import io
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.cache.cache_strategy import LastModifiedCacheStrategy
from resourcey.core.errors import (
    InvalidInputError,
    ResourceyConfigError,
    UnsupportedFilterError,
)
from resourcey.core.manifest import Manifest
from resourcey.core.service import Action, Delete, NotFoundError, Update
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_resource import FileResource, file_resource
from resourcey.filestore.file_routes import register_file_routes
from resourcey.filestore.file_store import FileStore
from resourcey.filestore.local_file_store import LocalFileStore
from resourcey.filestore.s3_file_store import S3FileStore
from resourcey.filestore.signed_url import mint_signed_url, verify_signed_url
from resourcey.filestore.sql_file_store import (
    FileBlobBase,
    SqlFileStore,
    create_blob_tables,
    sql_file_blob_view,
)
from resourcey.http.app import create_app

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
        stored = await local_store.put("abc123", b"hello", content_type="text/plain", name="a.txt")
        assert stored.size == 5
        assert stored.content_type == "text/plain"
        assert stored.name == "a.txt"
        assert stored.updated_at is not None

        assert await local_store.get("abc123") == b"hello"

        head = await local_store.head("abc123")
        assert head is not None
        assert head.size == 5
        assert head.content_type == "text/plain"
        assert head.name == "a.txt"

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

    async def test_presign_get_uses_framework_signing(self, local_store: LocalFileStore) -> None:
        cap = local_store.presign_get("abc123", expires_in_seconds=60)
        assert cap.method == "GET"
        assert "/_files/abc123" in cap.url
        assert "token=" in cap.url
        verified_key = local_store.verify(cap.url.split("token=")[1])
        assert verified_key == "abc123"

    async def test_list_objects_orders_ascending_and_pages(
        self, local_store: LocalFileStore
    ) -> None:
        for key in ("c", "a", "b"):
            await local_store.put(key, b"x")
        listed = await local_store.list_objects(limit=10)
        assert [o.key for o in listed] == ["a", "b", "c"]
        page = await local_store.list_objects(limit=1)
        assert [o.key for o in page] == ["a"]
        rest = await local_store.list_objects(after="a", limit=10)
        assert [o.key for o in rest] == ["b", "c"]

    async def test_list_objects_on_empty_root_is_empty(self, tmp_path: Any) -> None:
        store = LocalFileStore(root=tmp_path / "never-created")
        assert await store.list_objects(limit=10) == []
        assert await store.count_objects() == 0

    async def test_count_objects(self, local_store: LocalFileStore) -> None:
        assert await local_store.count_objects() == 0
        await local_store.put("a", b"x")
        await local_store.put("b", b"y")
        assert await local_store.count_objects() == 2


# ---------------------------------------------------------------------------
# SqlFileStore (real SQLite, blob table)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sql_store() -> AsyncIterator[SqlFileStore]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FileBlobBase.metadata.create_all)
    store = SqlFileStore(session_factory=maker)
    async with store:
        yield store
    await engine.dispose()


class TestSqlFileStore:
    async def test_put_get_head_delete_round_trip(self, sql_store: SqlFileStore) -> None:
        stored = await sql_store.put("k1", b"data", content_type="application/json", name="a.json")
        assert stored.size == 4
        assert await sql_store.get("k1") == b"data"
        head = await sql_store.head("k1")
        assert head is not None and head.content_type == "application/json"
        assert head.name == "a.json"
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

    async def test_list_objects_orders_ascending_and_pages(self, sql_store: SqlFileStore) -> None:
        for key in ("c", "a", "b"):
            await sql_store.put(key, b"x")
        listed = await sql_store.list_objects(limit=10)
        assert [o.key for o in listed] == ["a", "b", "c"]
        rest = await sql_store.list_objects(after="a", limit=10)
        assert [o.key for o in rest] == ["b", "c"]

    async def test_count_objects(self, sql_store: SqlFileStore) -> None:
        assert await sql_store.count_objects() == 0
        await sql_store.put("a", b"x")
        assert await sql_store.count_objects() == 1

    async def test_entry_resolves_a_session_maker_from_a_manager(self) -> None:
        from resourcey.sql.db_config import DbConfig
        from resourcey.sql.session_manager import SqlSessionManager
        from resourcey.sql.sql_config import SqlConfig

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


class TestSqlFileBlobView:
    async def test_data_is_hidden_and_actions_are_read_only(self) -> None:
        view = sql_file_blob_view()
        read_fields = view.get_rest_models().read_response.model_fields
        assert "data" not in read_fields
        assert "key" in read_fields
        assert view.get_supported_actions() == {
            Action.READ,
            Action.SEARCH,
            Action.COUNT,
            Action.BATCH_READ,
        }

    async def test_served_over_http_without_the_bytes(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(FileBlobBase.metadata.create_all)
        store = SqlFileStore(session_factory=maker)
        try:
            async with store:
                await store.put("k1", b"hello", name="a.txt")
                view = sql_file_blob_view(session_factory=maker)
                manifest = Manifest(resources=[view])
                app = create_app(manifest)
                async with manifest:
                    transport = ASGITransport(app=app)
                    async with AsyncClient(transport=transport, base_url="http://test") as c:
                        response = await c.get("/file-blobs/k1")
                        assert response.status_code == 200
                        assert "data" not in response.json()
                        assert response.json()["name"] == "a.txt"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# S3FileStore (stub client; no network)
# ---------------------------------------------------------------------------


class _StubS3Client:
    """A minimal in-memory stand-in for a boto3 S3 client."""

    class exceptions:  # noqa: N801 - mirrors boto3's namespace
        class NoSuchKey(Exception): ...  # noqa: N818 - mirrors boto3

        class ClientError(Exception): ...

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None, dict[str, str]]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kw: Any) -> None:  # noqa: N803
        self.objects[Key] = (Body, kw.get("ContentType"), kw.get("Metadata") or {})

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.ClientError(Key)
        body, content_type, metadata = self.objects[Key]
        return {
            "ContentLength": len(body),
            "ContentType": content_type,
            "ETag": '"stub"',
            "Metadata": metadata,
            "LastModified": datetime.now(UTC),
        }

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,  # noqa: N803 - mirrors boto3's keyword
        MaxKeys: int = 1000,  # noqa: N803
        Prefix: str = "",  # noqa: N803
        **kw: Any,
    ) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        after = kw.get("StartAfter")
        if after is not None:
            keys = [k for k in keys if k > after]
        page = keys[:MaxKeys]
        contents = [{"Key": k, "Size": len(self.objects[k][0]), "ETag": '"stub"'} for k in page]
        return {"Contents": contents, "KeyCount": len(contents), "IsTruncated": False}

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
        await s3_store.put("k1", b"abc", content_type="text/plain", name="a.txt")
        assert await s3_store.get("k1") == b"abc"
        head = await s3_store.head("k1")
        assert head is not None and head.etag == '"stub"'
        assert head.name == "a.txt"
        await s3_store.delete("k1")
        assert await s3_store.get("k1") is None

    async def test_native_presign_get(self, s3_store: S3FileStore) -> None:
        get = s3_store.presign_get("k1", expires_in_seconds=30)
        assert get.method == "GET"

    async def test_import_safe_without_boto3(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.setattr("resourcey.filestore.s3_file_store.S3_MAX_PUT_BYTES", 4)
        store = S3FileStore(bucket="b", client=_StubS3Client())
        with pytest.raises(ResourceyConfigError, match="single-PUT cap"):
            await store.put("k", b"12345")

    async def test_client_property_exposes_the_stub(self, s3_store: S3FileStore) -> None:
        assert s3_store.client is not None

    async def test_list_objects_strips_the_prefix(self, s3_store: S3FileStore) -> None:
        await s3_store.put("a", b"x")
        await s3_store.put("b", b"y")
        listed = await s3_store.list_objects(limit=10)
        assert sorted(o.key for o in listed) == ["a", "b"]
        # ListObjectsV2 cannot report per-object metadata.
        assert all(o.name is None for o in listed)

    async def test_count_objects(self, s3_store: S3FileStore) -> None:
        assert await s3_store.count_objects() == 0
        await s3_store.put("a", b"x")
        assert await s3_store.count_objects() == 1

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
# Signed download URLs: mint / verify, expiry, key binding
# ---------------------------------------------------------------------------


class TestSignedUrl:
    def test_round_trip(self) -> None:
        url = mint_signed_url(_encryption(), "key-1", expires_in_seconds=60)
        token = url.url.split("token=")[1]
        assert verify_signed_url(_encryption(), token) == "key-1"

    def test_expiry_is_enforced_by_the_verifier(self) -> None:
        url = mint_signed_url(_encryption(), "key-1", expires_in_seconds=-1)
        token = url.url.split("token=")[1]
        with pytest.raises(InvalidInputError, match="expired"):
            verify_signed_url(_encryption(), token)

    def test_tampered_token_is_rejected(self) -> None:
        with pytest.raises(InvalidInputError):
            verify_signed_url(_encryption(), "not-a-token")

    def test_token_without_key_claim_is_rejected(self) -> None:
        service = _encryption()
        token = service.create_jwe_token({}, expires_in=timedelta(seconds=60))
        with pytest.raises(InvalidInputError, match="key claim"):
            verify_signed_url(service, token)

    def test_token_without_expiry_is_rejected(self) -> None:
        service = _encryption()
        token = service.create_jwe_token({"k": "k"}, expires_in=timedelta(seconds=60))
        original = service.decrypt_jwe_token

        def _no_exp(t: str) -> dict[str, object]:
            claims = dict(original(t))
            claims.pop("exp", None)
            return claims

        service.decrypt_jwe_token = _no_exp  # type: ignore[method-assign]
        try:
            with pytest.raises(InvalidInputError, match="no expiry"):
                verify_signed_url(service, token)
        finally:
            service.decrypt_jwe_token = original  # type: ignore[method-assign]

    def test_verify_binds_the_key(self) -> None:
        store = LocalFileStore()
        cap = store.presign_get("k-1", expires_in_seconds=60)
        token = cap.url.split("token=")[1]
        assert store.verify(token) == "k-1"


# ---------------------------------------------------------------------------
# ``files`` resource: DTO / cache strategy / query surface / action layer
# ---------------------------------------------------------------------------


class TestFileResource:
    def test_create_request_has_no_client_input(self) -> None:
        """Every field is server-derived from the upload -- nothing is left to declare."""
        resource = file_resource(LocalFileStore())
        create = resource.get_rest_models().create_request.model_fields
        assert set(create) == set()

    def test_create_response_shape(self) -> None:
        resource = file_resource(LocalFileStore())
        create_response = resource.get_rest_models().create_response.model_fields
        assert {"id", "name", "content_type", "size", "checksum", "etag", "updated_at"} <= set(
            create_response
        )
        assert "content" not in create_response

    def test_content_field_never_reaches_a_rest_shape(self) -> None:
        """``content`` carries bytes transport -> service, in-process only."""
        resource = file_resource(LocalFileStore())
        models = resource.get_rest_models()
        assert "content" not in models.read_response.model_fields
        assert "content" not in models.search_response.model_fields
        assert "content" not in models.create_response.model_fields
        assert "content" not in models.create_request.model_fields

    def test_read_response_shape(self) -> None:
        resource = file_resource(LocalFileStore())
        read = resource.get_rest_models().read_response.model_fields
        assert {"id", "name", "content_type", "size", "checksum", "etag", "updated_at"} <= set(read)

    def test_search_response_omits_declared_metadata(self) -> None:
        resource = file_resource(LocalFileStore())
        search = resource.get_rest_models().search_response.model_fields
        assert "name" not in search
        assert "content_type" not in search
        assert "checksum" not in search
        assert {"id", "size", "etag", "updated_at"} <= set(search)

    def test_cache_strategy_is_last_modified(self) -> None:
        resource = file_resource(LocalFileStore())
        assert isinstance(resource.get_cache_strategy(), LastModifiedCacheStrategy)

    def test_no_queryable_or_sortable_fields(self) -> None:
        resource = file_resource(LocalFileStore())
        assert resource.get_queryable_fields() == frozenset()
        assert resource.get_sortable_fields() == frozenset()
        assert resource.get_filter_operators() == {}

    def test_resolve_sort_order_rejects_any_sort_field(self) -> None:
        resource = file_resource(LocalFileStore())
        assert resource.resolve_sort_order(None, False) is None
        with pytest.raises(InvalidInputError):
            resource.resolve_sort_order("size", False)

    def test_no_update_action(self) -> None:
        resource = file_resource(LocalFileStore())
        assert Action.UPDATE not in resource.get_supported_actions()

    def test_no_create_action(self) -> None:
        """create is a hand-written multipart route, not a declared action."""
        resource = file_resource(LocalFileStore())
        assert Action.CREATE not in resource.get_supported_actions()

    def test_max_size_property_reflects_the_constructor_argument(self) -> None:
        resource = FileResource(LocalFileStore(), max_size=10)
        assert resource.max_size == 10
        assert FileResource(LocalFileStore()).max_size is None


@pytest_asyncio.fixture
async def file_service(tmp_path: Any) -> AsyncIterator[Any]:
    store = LocalFileStore(root=tmp_path / "blobs")
    resource = FileResource(store)
    async with store:
        service = await resource.get_service({})
        async with service:
            yield service


class TestFileService:
    async def test_create_commits_the_upload_immediately(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        created = await file_service.create(
            dto_type(name="a.txt", content_type="text/plain", content=b"hello")
        )
        assert created.size == 5
        assert created.checksum == hashlib.sha256(b"hello").hexdigest()
        # No separate transfer step -- the file already exists.
        read = await file_service.read(created.id)
        assert read.name == "a.txt"
        assert read.size == 5

    async def test_size_and_checksum_are_computed_not_trusted(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        created = await file_service.create(dto_type(name="a.txt", content=b"hello world"))
        assert created.size == len(b"hello world")
        assert created.checksum == hashlib.sha256(b"hello world").hexdigest()

    async def test_content_type_defaults_when_absent(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        created = await file_service.create(dto_type(name="a.txt", content=b"x"))
        assert created.content_type == "application/octet-stream"

    async def test_full_round_trip_through_the_medium(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        created = await file_service.create(
            dto_type(name="a.txt", content_type="text/plain", content=b"hello")
        )

        page = await file_service.search()
        assert len(page.items) == 1
        assert page.items[0].id == created.id

        assert await file_service.count() == 1

        batch = await file_service.batch_read([created.id, "missing"])
        assert batch[0].id == created.id
        assert batch[1] is None

        await file_service.delete(created.id)
        with pytest.raises(NotFoundError):
            await file_service.read(created.id)

    async def test_delete_of_absent_raises_not_found(self, file_service: Any) -> None:
        with pytest.raises(NotFoundError):
            await file_service.delete("ghost")

    async def test_max_size_cap_rejects_an_oversize_upload(self, tmp_path: Any) -> None:
        store = LocalFileStore(root=tmp_path / "blobs")
        resource = FileResource(store, max_size=4)
        async with store:
            service = await resource.get_service({})
            async with service:
                with pytest.raises(InvalidInputError, match="exceeding the 4-byte cap"):
                    await service.create(
                        resource.get_dto_type()(
                            name="big.bin",
                            content_type="application/octet-stream",
                            content=b"hello",
                        )
                    )

    async def test_search_rejects_a_filter(self, file_service: Any) -> None:
        from resourcey.util.search_filter import AllFilter

        with pytest.raises(UnsupportedFilterError):
            await file_service.search(search_filter=AllFilter())

    async def test_count_rejects_a_filter(self, file_service: Any) -> None:
        from resourcey.util.search_filter import AllFilter

        with pytest.raises(UnsupportedFilterError):
            await file_service.count(search_filter=AllFilter())

    async def test_search_pages_with_a_cursor(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        for name in ("a", "b", "c"):
            await file_service.create(dto_type(name=name, content=b"x"))
        page1 = await file_service.search(limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        page2 = await file_service.search(limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.next_cursor is None

    async def test_invalid_cursor_is_rejected(self, file_service: Any) -> None:
        with pytest.raises(InvalidInputError):
            await file_service.search(cursor="not-a-real-cursor")

    async def test_batch_edit_create_is_rejected(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        from resourcey.core.service import Create

        with pytest.raises(InvalidInputError, match="batch_edit cannot create"):
            await file_service.batch_edit([Create(item=dto_type(name="a.txt", content=b"x"))])

    async def test_batch_edit_delete(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        created = await file_service.create(dto_type(name="a.txt", content=b"x"))
        results = await file_service.batch_edit([Delete(id=created.id), Delete(id="ghost")])
        assert results == [None, None]
        with pytest.raises(NotFoundError):
            await file_service.read(created.id)

    async def test_batch_edit_update_is_rejected(self, file_service: Any) -> None:
        dto_type = file_service._resource.get_dto_type()
        with pytest.raises(InvalidInputError, match="no update action"):
            await file_service.batch_edit([Update(item=dto_type(id="x"))])


# ---------------------------------------------------------------------------
# End-to-end over HTTP (Local medium)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path: Any) -> AsyncIterator[AsyncClient]:
    store = LocalFileStore(root=tmp_path / "blobs")
    resource = FileResource(store)
    manifest = Manifest(resources=[], managers=[store])
    app = create_app(manifest)
    register_file_routes(app, store, resource=resource, config=FileStoreConfig())

    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _create(client: AsyncClient, *, name: str = "notes.txt", size: int = 5) -> str:
    created = await client.post("/files", files={"file": (name, b"h" * size, "text/plain")})
    assert created.status_code == 201
    return str(created.json()["id"])


async def test_happy_path(client: AsyncClient) -> None:
    file_id = await _create(client)

    read = await client.get(f"/files/{file_id}")
    assert read.status_code == 200
    assert read.json()["content_type"] == "text/plain"
    assert read.json()["etag"]

    search = await client.get("/files")
    assert search.status_code == 200
    assert len(search.json()["items"]) == 1
    assert "name" not in search.json()["items"][0]

    count = await client.get("/files/count")
    assert count.status_code == 200
    assert count.json() == 1

    download = await client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    get_url = download.json()["url"]
    fetched = await client.get(get_url)
    assert fetched.status_code == 200
    assert fetched.content == b"hhhhh"

    content = await client.get(f"/files/{file_id}/content")
    assert content.status_code == 200
    assert content.content == b"hhhhh"
    assert content.headers["content-type"].startswith("text/plain")

    deleted = await client.delete(f"/files/{file_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/files/{file_id}")).status_code == 404
    assert (await client.get(f"/files/{file_id}/content")).status_code == 404


async def test_create_returns_201_with_the_computed_fields(client: AsyncClient) -> None:
    created = await client.post("/files", files={"file": ("a.txt", b"hello", "text/plain")})
    assert created.status_code == 201
    body = created.json()
    assert body["size"] == 5
    assert body["checksum"] == hashlib.sha256(b"hello").hexdigest()


async def test_read_is_cacheable(client: AsyncClient) -> None:
    file_id = await _create(client)
    fetched = await client.get(f"/files/{file_id}")
    assert fetched.status_code == 200
    last_modified = fetched.headers.get("last-modified")
    assert last_modified
    conditional = await client.get(
        f"/files/{file_id}", headers={"if-modified-since": last_modified}
    )
    assert conditional.status_code == 304


async def test_read_of_a_missing_id_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/files/{uuid4().hex}")
    assert response.status_code == 404


async def test_content_of_a_missing_id_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/files/{uuid4().hex}/content")
    assert response.status_code == 404


async def test_download_of_a_missing_id_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/files/{uuid4().hex}/download")
    assert response.status_code == 404


async def test_filtering_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/files?name__eq=x")
    assert response.status_code == 400


async def test_sorting_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/files?sort=size")
    assert response.status_code == 400


async def test_batch_edit_cannot_create(client: AsyncClient) -> None:
    response = await client.post(
        "/files/batch-edit",
        json=[{"kind": "Create", "item": {"name": "a.txt"}}],
    )
    # The batch-edit kind union is narrowed to ``Delete`` only -- an
    # unrecognized discriminator tag is a validation error.
    assert response.status_code == 422


async def test_batch_edit_can_delete(client: AsyncClient) -> None:
    file_id = await _create(client)
    delete_response = await client.post(
        "/files/batch-edit",
        json=[{"kind": "Delete", "id": file_id}],
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == [None]
    assert (await client.get(f"/files/{file_id}")).status_code == 404


async def test_signed_url_is_bound_to_its_object(client: AsyncClient) -> None:
    file_id = await _create(client)
    other_id = await _create(client, name="other.txt")
    download = await client.get(f"/files/{file_id}/download")
    token = download.json()["url"].split("token=")[1]
    wrong = await client.get(f"/_files/{other_id}?token={token}")
    assert wrong.status_code == 400


async def test_missing_token_on_transfer_is_rejected(client: AsyncClient) -> None:
    response = await client.get(f"/_files/{uuid4().hex}")
    assert response.status_code == 400


async def test_signed_get_of_an_absent_object_is_404(client: AsyncClient) -> None:
    token = mint_signed_url(get_encryption_service(), "ghost", expires_in_seconds=60).url.split(
        "token="
    )[1]
    response = await client.get(f"/_files/ghost?token={token}")
    assert response.status_code == 404


async def test_register_file_routes_rejects_a_resource_already_on_a_manifest() -> None:
    store = LocalFileStore()
    resource = FileResource(store)
    Manifest(resources=[resource], managers=[store])  # registers on_register
    app = FastAPI()
    with pytest.raises(ResourceyConfigError, match="already registered"):
        register_file_routes(app, store, resource=resource)


# ---------------------------------------------------------------------------
# End-to-end over HTTP (S3 medium: content route redirects)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def s3_fixture() -> AsyncIterator[tuple[AsyncClient, S3FileStore]]:
    store = S3FileStore(bucket="bucket", client=_StubS3Client())
    resource = FileResource(store)
    manifest = Manifest(resources=[], managers=[store])
    app = create_app(manifest)
    register_file_routes(app, store, resource=resource, config=FileStoreConfig())
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, store


async def test_create_proxies_the_upload_to_s3(
    s3_fixture: tuple[AsyncClient, S3FileStore],
) -> None:
    client, store = s3_fixture
    created = await client.post("/files", files={"file": ("a.txt", b"hello", "text/plain")})
    assert created.status_code == 201
    file_id = created.json()["id"]
    assert await store.get(file_id) == b"hello"


async def test_content_route_404s_for_a_missing_id(
    s3_fixture: tuple[AsyncClient, S3FileStore],
) -> None:
    client, _store = s3_fixture
    response = await client.get(f"/files/{uuid4().hex}/content", follow_redirects=False)
    assert response.status_code == 404


async def test_content_route_redirects_for_s3(
    s3_fixture: tuple[AsyncClient, S3FileStore],
) -> None:
    client, _store = s3_fixture
    created = await client.post("/files", files={"file": ("a.txt", b"hello", "text/plain")})
    file_id = created.json()["id"]

    response = await client.get(f"/files/{file_id}/content", follow_redirects=False)
    assert response.status_code == 307
    assert "s3.example" in response.headers["location"]

    download = await client.get(f"/files/{file_id}/download")
    assert download.status_code == 200
    assert download.json()["method"] == "GET"


# ---------------------------------------------------------------------------
# FileStore typing / config
# ---------------------------------------------------------------------------


class TestFileStoreConfig:
    def test_defaults_and_ttl_properties(self) -> None:
        config = FileStoreConfig()
        assert config.download_url_ttl == timedelta(seconds=config.download_url_ttl_seconds)
        assert config.download_url_ttl_seconds > 0
        assert config.max_size is None

    def test_default_medium_is_local(self) -> None:
        assert isinstance(FileStoreConfig().medium, FileStore)
