"""Tests for ``SecretStr`` encryption at rest (issue #202).

A ``SecretStr`` field is encrypted when written to storage and decrypted when
read back, on both the SQL and Mongo backends. The stored value is read raw
(bypassing the service) to prove no plaintext reaches storage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel, SecretStr
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.dto import DTO, secret_field_names, secret_fields_of_model
from resourcey.core.errors import UnsupportedFilterError
from resourcey.core.manifest import Manifest
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_fields import decrypt_fields, encrypt_fields
from resourcey.encryption.encryption_service import EncryptionService
from resourcey.http.app import create_app
from resourcey.mongo.mongo_client import MongoClientManager
from resourcey.mongo.mongo_config import MongoConfig, MongoConnectionConfig
from resourcey.mongo.mongo_resource import MongoResource
from resourcey.sql.sql_resource import SqlResource
from resourcey.util.search_filter import EqFilter, attr
from resourcey.view.resource_view import ResourceView

PLAINTEXT = "PLAINTEXT-CANARY-4242"


def _enc(key_id: str = "k1", value: str = "secret-one", *old: tuple[str, str]) -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id=key_id, value=value),
            decryption_keys=[EncryptionKeyConfig(id=i, value=v) for i, v in old],
        )
    )


def _is_jwe(value: str) -> bool:
    return len(value.split(".")) == 5 and PLAINTEXT not in value


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class _Declared(DTO):
    id: UUID
    plain: str
    token: SecretStr
    maybe: SecretStr | None = None


def test_secret_field_names_detects_plain_and_nullable_secrets():
    assert secret_field_names(_Declared) == frozenset({"token", "maybe"})


def test_secret_fields_of_model_reads_the_response_annotations():
    read = _Declared.get_rest_models().read_response
    assert secret_fields_of_model(read) == frozenset({"token", "maybe"})

    class Other(BaseModel):
        a: str

    assert secret_fields_of_model(Other) == frozenset()


def test_nullable_secret_is_redacted_on_dump():
    dumped = (
        _Declared.get_rest_models()
        .read_response(id=uuid4(), plain="p", token=SecretStr("a"), maybe=SecretStr("b"))
        .model_dump()
    )
    assert dumped["maybe"] == "**********"


def test_is_ciphertext_distinguishes_ciphertext_from_plaintext():
    enc = _enc()
    assert enc.is_ciphertext(enc.encrypt_value("x"))
    assert not enc.is_ciphertext("x")
    assert not enc.is_ciphertext("a.b.c.d.e")


def test_encrypt_and_decrypt_fields_round_trip_and_skip_none():
    enc = _enc()
    record = {"a": SecretStr("one"), "b": None, "c": "untouched"}
    stored = encrypt_fields(enc, record, {"a", "b"})
    assert stored["b"] is None
    assert stored["c"] == "untouched"
    assert enc.is_ciphertext(stored["a"])
    assert decrypt_fields(enc, stored, {"a", "b"}) == {"a": "one", "b": None, "c": "untouched"}


def test_decrypt_fields_passes_legacy_plaintext_through():
    assert decrypt_fields(_enc(), {"a": "legacy"}, {"a", "absent"}) == {"a": "legacy"}


def test_decrypt_fields_refuses_ciphertext_under_an_unknown_key():
    stored = encrypt_fields(_enc("old", "old-secret"), {"a": "x"}, {"a"})
    with pytest.raises(ValueError, match="not found"):
        decrypt_fields(_enc("new", "new-secret"), stored, {"a"})


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class Cred(_Base):
    __tablename__ = "creds"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(100), default="l")
    api_secret: Mapped[SecretStr] = mapped_column(String(2048))
    note: Mapped[SecretStr | None] = mapped_column(String(2048), nullable=True)


class DigestCredResource(SqlResource[Any, Any]):
    """Opts the secret column out: it holds a digest, not a recoverable value."""

    def get_encrypted_fields(self) -> frozenset[str]:
        return frozenset()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _sql_resource(engine: Any, enc: EncryptionService | None = None, cls: Any = SqlResource) -> Any:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return cls(Cred, session_factory=maker, encryption_service=enc or _enc())


async def _raw_row(engine: Any, cred_id: UUID) -> Any:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT label, api_secret, note FROM creds WHERE id = :i"),
            {"i": cred_id.hex},
        )
        return result.one()


async def _create(resource: Any, **values: Any) -> Any:
    values.setdefault("api_secret", SecretStr(PLAINTEXT))
    async with await resource.get_service() as service:
        return await service.create(resource.get_dto_type()(**values))


async def test_sql_secret_is_ciphertext_at_rest_and_plaintext_through_the_service(engine):
    resource = _sql_resource(engine)
    created = await _create(resource, note=SecretStr("n"))
    row = await _raw_row(engine, created.id)
    assert _is_jwe(row.api_secret)
    assert _enc().decrypt_value(row.api_secret) == PLAINTEXT
    assert _enc().decrypt_value(row.note) == "n"
    assert row.label == "l"
    assert created.api_secret.get_secret_value() == PLAINTEXT
    async with await resource.get_service() as service:
        assert (await service.read(created.id)).api_secret.get_secret_value() == PLAINTEXT


async def test_sql_encryption_is_non_deterministic(engine):
    resource = _sql_resource(engine)
    first = await _create(resource)
    second = await _create(resource)
    first_raw = (await _raw_row(engine, first.id)).api_secret
    second_raw = (await _raw_row(engine, second.id)).api_secret
    assert first_raw != second_raw


async def test_sql_search_batch_read_and_update_round_trip(engine):
    resource = _sql_resource(engine)
    created = await _create(resource)
    dto = resource.get_dto_type()
    async with await resource.get_service() as service:
        page = await service.search()
        assert [i.api_secret.get_secret_value() for i in page.items] == [PLAINTEXT]
        [batched] = await service.batch_read([created.id])
        assert batched.api_secret.get_secret_value() == PLAINTEXT
        updated = await service.update(dto(id=created.id, api_secret=SecretStr("rotated")))
        assert updated.api_secret.get_secret_value() == "rotated"
        relabelled = await service.update(dto(id=created.id, label="other"))
        assert relabelled.api_secret.get_secret_value() == "rotated"
    row = await _raw_row(engine, created.id)
    assert "rotated" not in row.api_secret
    assert _enc().decrypt_value(row.api_secret) == "rotated"


async def test_sql_null_secret_stays_null(engine):
    resource = _sql_resource(engine)
    created = await _create(resource)
    assert created.note is None
    assert (await _raw_row(engine, created.id)).note is None


async def test_sql_row_encrypted_under_an_old_key_still_reads_after_rotation(engine):
    created = await _create(_sql_resource(engine, _enc("k1", "secret-one")))
    rotated = _sql_resource(engine, _enc("k2", "secret-two", ("k1", "secret-one")))
    async with await rotated.get_service() as service:
        assert (await service.read(created.id)).api_secret.get_secret_value() == PLAINTEXT
        await service.update(rotated.get_dto_type()(id=created.id, api_secret=SecretStr("new")))
        assert (await service.read(created.id)).api_secret.get_secret_value() == "new"
    stored = (await _raw_row(engine, created.id)).api_secret
    assert _enc("k2", "secret-two").decrypt_value(stored) == "new"


async def test_sql_row_under_a_dropped_key_fails_loudly(engine):
    created = await _create(_sql_resource(engine, _enc("k1", "secret-one")))
    other = _sql_resource(engine, _enc("k2", "secret-two"))
    async with await other.get_service() as service:
        with pytest.raises(ValueError, match="not found"):
            await service.read(created.id)


async def test_sql_legacy_plaintext_row_reads_and_is_encrypted_on_next_secret_write(engine):
    resource = _sql_resource(engine)
    legacy_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO creds (id, label, api_secret) VALUES (:i, 'old', :s)"),
            {"i": legacy_id.hex, "s": PLAINTEXT},
        )
    dto = resource.get_dto_type()
    async with await resource.get_service() as service:
        assert (await service.read(legacy_id)).api_secret.get_secret_value() == PLAINTEXT
        await service.update(dto(id=legacy_id, label="touched"))
    assert (await _raw_row(engine, legacy_id)).api_secret == PLAINTEXT
    async with await resource.get_service() as service:
        await service.update(dto(id=legacy_id, api_secret=SecretStr(PLAINTEXT)))
    assert _is_jwe((await _raw_row(engine, legacy_id)).api_secret)


async def test_sql_opted_out_field_is_stored_verbatim(engine):
    resource = _sql_resource(engine, cls=DigestCredResource)
    created = await _create(resource)
    assert (await _raw_row(engine, created.id)).api_secret == PLAINTEXT
    async with await resource.get_service() as service:
        assert (await service.read(created.id)).api_secret.get_secret_value() == PLAINTEXT


async def test_sql_secret_fields_are_not_queryable_or_sortable(engine):
    resource = _sql_resource(engine)
    assert resource.get_queryable_fields() == frozenset({"id", "label"})
    assert resource.get_sortable_fields() == frozenset({"id", "label"})
    assert set(resource.get_filter_operators()) == {"id", "label"}
    with pytest.raises(UnsupportedFilterError):
        resource.build_filter_context().column_for("api_secret")
    async with await resource.get_service() as service:
        with pytest.raises(UnsupportedFilterError):
            await service.search(attr("api_secret", EqFilter(value=PLAINTEXT)))


async def test_sql_http_filter_and_sort_on_a_secret_are_400(engine):
    resource = _sql_resource(engine)
    await _create(resource)
    manifest = Manifest(resources=(resource,))
    app = create_app(manifest)
    async with (
        manifest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        by_secret = await client.get("/creds", params={"api_secret__eq": PLAINTEXT})
        assert by_secret.status_code == 400
        assert (await client.get("/creds", params={"sort": "api_secret"})).status_code == 400
        assert (await client.get("/creds", params={"label__eq": "l"})).status_code == 200


async def test_view_over_an_encrypting_resource_hides_secrets_from_the_query_surface(engine):
    view = ResourceView(_sql_resource(engine))
    assert "api_secret" not in view.get_queryable_fields()
    assert "api_secret" not in view.get_filter_operators()


# ---------------------------------------------------------------------------
# Mongo
# ---------------------------------------------------------------------------


class MongoCredDTO(DTO):
    id: UUID
    label: str = "l"
    api_secret: SecretStr
    note: SecretStr | None = None


class MongoDigestResource(MongoResource[Any, Any]):
    def get_encrypted_fields(self) -> frozenset[str]:
        return frozenset()


class MigratingCredResource(MongoResource[Any, Any]):
    def migrate_document(self, document: dict[str, Any]) -> dict[str, Any]:
        document = dict(document)
        if "secret" in document:
            document["api_secret"] = document.pop("secret")
        return document


class IteratingCredResource(MongoResource[Any, Any]):
    allow_filter_iteration = True


class _SeenEq(EqFilter[Any]):
    """A filter node with no Mongo rule, so the iteration fallback evaluates it in memory."""

    def matches(self, value: Any) -> bool:
        SEEN.append(value.api_secret.get_secret_value())
        return True


SEEN: list[str] = []


def _mongo(cls: Any = MongoResource, enc: EncryptionService | None = None) -> tuple[Any, Any]:
    manager = MongoClientManager(
        MongoConfig(
            mongo_connections=[MongoConnectionConfig(name="main", url="embedded://secret_tests")]
        )
    )
    return manager, cls(MongoCredDTO, client_manager=manager, encryption_service=enc or _enc())


@pytest_asyncio.fixture
async def mongo() -> AsyncIterator[Any]:
    manager, resource = _mongo()
    async with manager, resource:
        yield resource


async def _raw_doc(resource: Any, cred_id: UUID) -> dict[str, Any]:
    return await resource._collection.find_one({"_id": str(cred_id)})


async def test_mongo_secret_is_ciphertext_at_rest_and_plaintext_through_the_service(mongo):
    created = await _create(mongo, id=uuid4(), note=SecretStr("n"))
    doc = await _raw_doc(mongo, created.id)
    assert _is_jwe(doc["api_secret"])
    assert _enc().decrypt_value(doc["api_secret"]) == PLAINTEXT
    assert _enc().decrypt_value(doc["note"]) == "n"
    assert doc["label"] == "l"
    assert created.api_secret.get_secret_value() == PLAINTEXT
    async with await mongo.get_service() as service:
        assert (await service.read(created.id)).api_secret.get_secret_value() == PLAINTEXT


async def test_mongo_search_batch_read_and_update_round_trip(mongo):
    created = await _create(mongo, id=uuid4())
    dto = mongo.get_dto_type()
    async with await mongo.get_service() as service:
        page = await service.search()
        assert [i.api_secret.get_secret_value() for i in page.items] == [PLAINTEXT]
        [batched] = await service.batch_read([created.id])
        assert batched.api_secret.get_secret_value() == PLAINTEXT
        updated = await service.update(dto(id=created.id, api_secret=SecretStr("rotated")))
        assert updated.api_secret.get_secret_value() == "rotated"
    doc = await _raw_doc(mongo, created.id)
    assert "rotated" not in doc["api_secret"]
    assert _enc().decrypt_value(doc["api_secret"]) == "rotated"


async def test_mongo_null_secret_stays_null(mongo):
    created = await _create(mongo, id=uuid4())
    assert (await _raw_doc(mongo, created.id))["note"] is None


async def test_mongo_row_encrypted_under_an_old_key_still_reads_after_rotation():
    manager, first = _mongo(enc=_enc("k1", "secret-one"))
    async with manager, first:
        created = await _create(first, id=uuid4())
        rotated = MongoResource(
            MongoCredDTO,
            client_manager=manager,
            encryption_service=_enc("k2", "secret-two", ("k1", "secret-one")),
        )
        async with rotated, await rotated.get_service() as service:
            assert (await service.read(created.id)).api_secret.get_secret_value() == PLAINTEXT


async def test_mongo_legacy_plaintext_document_reads_and_is_encrypted_on_next_secret_write(mongo):
    legacy_id = uuid4()
    await mongo._collection.insert_one(
        {"_id": str(legacy_id), "label": "old", "api_secret": PLAINTEXT}
    )
    async with await mongo.get_service() as service:
        assert (await service.read(legacy_id)).api_secret.get_secret_value() == PLAINTEXT
        await service.update(mongo.get_dto_type()(id=legacy_id, api_secret=SecretStr(PLAINTEXT)))
    assert _is_jwe((await _raw_doc(mongo, legacy_id))["api_secret"])


async def test_mongo_migrated_secret_field_is_decrypted_under_its_new_name():
    manager, resource = _mongo(MigratingCredResource)
    async with manager, resource:
        legacy_id = uuid4()
        await resource._collection.insert_one(
            {"_id": str(legacy_id), "label": "old", "secret": _enc().encrypt_value(PLAINTEXT)}
        )
        async with await resource.get_service() as service:
            assert (await service.read(legacy_id)).api_secret.get_secret_value() == PLAINTEXT


async def test_mongo_iteration_fallback_evaluates_decrypted_values():
    manager, resource = _mongo(IteratingCredResource)
    async with manager, resource:
        created = await _create(resource, id=uuid4())
        SEEN.clear()
        async with await resource.get_service() as service:
            page = await service.search(_SeenEq(value=1))
        assert [i.id for i in page.items] == [created.id]
        assert SEEN == [PLAINTEXT]


async def test_mongo_opted_out_field_is_stored_verbatim():
    manager, resource = _mongo(MongoDigestResource)
    async with manager, resource:
        created = await _create(resource, id=uuid4())
        assert (await _raw_doc(resource, created.id))["api_secret"] == PLAINTEXT


async def test_mongo_secret_fields_are_not_queryable_or_sortable(mongo):
    assert mongo.get_queryable_fields() == frozenset({"id", "label"})
    assert mongo.get_sortable_fields() == frozenset({"id", "label"})
    assert set(mongo.get_filter_operators()) == {"id", "label"}
    with pytest.raises(UnsupportedFilterError):
        mongo.build_filter_context().field_for("api_secret")
