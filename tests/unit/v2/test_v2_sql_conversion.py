"""Tests for ``sqlalchemy_2_dto`` — inferring a ``v2`` DTO from an ORM model.

The SQL workflow is model-first (issue #89): a developer defines the SQLAlchemy
model and :class:`~resourcey.v2.sql.resource.SqlResource` infers the DTO from
it. These tests cover the inference (column types, nullability, defaults, the
primary key, explicit ``DtoField`` overrides via ``column.info``), and the
resulting resource round-tripping the standard actions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    LargeBinary,
    Numeric,
    String,
    Time,
    Uuid,
    inspect,
    select,
    text,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.dto import DtoField
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.sql.resource import SqlResource
from resourcey.v2.sql.sqlalchemy_2_dto import sqlalchemy_2_dto


class Kind(StrEnum):
    ALPHA = "alpha"
    BETA = "beta"


class Choice(StrEnum):
    A = "a"
    B = "b"


class AdoptedBase(DeclarativeBase):
    pass


class Widget(AdoptedBase):
    """A representative model: int autoincrement PK, nullable, defaults, enum, JSON."""

    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))
    nickname: Mapped[str | None] = mapped_column(String(50), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    source: Mapped[str] = mapped_column(String(10), server_default="app")
    kind: Mapped[Kind] = mapped_column(SqlEnum(Kind))
    payload: Mapped[dict] = mapped_column(JSON)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[Any] = mapped_column(Numeric(10, 2), nullable=True)
    ref: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-cursors")
        )
    )


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-cursors")
        )
    )


def _maker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# sqlalchemy_2_dto — model -> DTO inference
# ---------------------------------------------------------------------------


def test_conversion_uses_the_model_name_by_default_and_allows_an_override():
    assert sqlalchemy_2_dto(Widget).__name__ == "Widget"
    assert sqlalchemy_2_dto(Widget, name="Renamed").__name__ == "Renamed"


def test_conversion_maps_the_primary_key_to_the_identifier():
    dto = sqlalchemy_2_dto(Widget)
    assert dto.id_field_name == "id"
    # The conventional id is server-generated and so not client-supplied on create.
    assert dto.get_fields()["id"].in_create_request is False
    assert dto.get_fields()["id"].in_update_request is False


def test_conversion_maps_column_types_to_python_annotations():
    dto = sqlalchemy_2_dto(Widget)
    fields = {name: ann for name, (ann, _cfg) in dto.__dto_fields__.items()}
    assert (fields["id"] | None) == (int | None)
    assert (fields["name"] | None) == (str | None)
    assert (fields["enabled"] | None) == (bool | None)
    assert (fields["kind"] | None) == (Kind | None)
    assert (fields["payload"] | None) == (dict | None)
    assert (fields["score"] | None) == (float | None)
    assert (fields["ref"] | None) == (UUID | None)
    assert (fields["created_at"] | None) == (datetime | None)


def test_conversion_maps_the_remaining_column_types():
    class Misc(AdoptedBase):
        __tablename__ = "misc"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        when: Mapped[Any] = mapped_column(Date, nullable=True)
        at: Mapped[Any] = mapped_column(Time, nullable=True)
        blob: Mapped[Any] = mapped_column(LargeBinary, nullable=True)

    dto = sqlalchemy_2_dto(Misc)
    fields = {name: ann for name, (ann, _cfg) in dto.__dto_fields__.items()}
    assert (fields["when"] | None) == (date | None)
    assert (fields["at"] | None) == (time | None)
    assert (fields["blob"] | None) == (bytes | None)


def test_conversion_unknown_column_type_falls_back_to_any():
    class Odd(AdoptedBase):
        __tablename__ = "odds"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        span: Mapped[Any] = mapped_column(Interval, nullable=True)

    dto = sqlalchemy_2_dto(Odd)
    ann = dto.__dto_fields__["span"][0]
    assert type(None) in getattr(ann, "__args__", ())


def test_nullability_becomes_an_optional_annotation():
    dto = sqlalchemy_2_dto(Widget)
    fields = {name: ann for name, (ann, _cfg) in dto.__dto_fields__.items()}
    # nickname and amount are nullable; name is not.
    assert type(None) in getattr(fields["nickname"], "__args__", ())
    assert type(None) in getattr(fields["amount"], "__args__", ())
    assert type(None) not in getattr(fields["name"], "__args__", ())


def test_client_side_defaults_become_create_defaults():
    dto = sqlalchemy_2_dto(Widget)
    fields = dto.get_fields()
    assert fields["score"].default_for_create == 0.0
    assert fields["score"].in_create_request is False
    assert fields["enabled"].in_create_request is False
    # A callable default becomes a create-default factory (unwrapped from the
    # SQLAlchemy ``(ctx)`` adapter so it takes no arguments).
    assert isinstance(fields["created_at"].default_factory_for_create(), datetime)
    assert isinstance(fields["ref"].default_factory_for_create(), UUID)
    # No update default without an ``onupdate``.
    assert fields["score"].has_default_for("update") is False


def test_onupdate_becomes_the_update_default():
    class WithOnUpdate(AdoptedBase):
        __tablename__ = "with_onupdate"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        touched: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), default=datetime.now, onupdate=datetime.now
        )

    field = sqlalchemy_2_dto(WithOnUpdate).get_fields()["touched"]
    assert field.has_default_for("create") is True
    assert field.has_default_for("update") is True


def test_server_default_drops_from_create_without_a_default():
    dto = sqlalchemy_2_dto(Widget)
    field = dto.get_fields()["source"]
    assert field.in_create_request is False
    assert field.has_default_for("create") is False


def test_nullable_column_without_a_default_gets_a_none_create_default():
    class Nullable(AdoptedBase):
        __tablename__ = "nullable_defaults"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        note: Mapped[str | None] = mapped_column(String(50), nullable=True)

    field = sqlalchemy_2_dto(Nullable).get_fields()["note"]
    assert field.default_for_create is None
    assert field.in_create_request is True


def test_a_natural_key_primary_key_stays_client_supplied():
    class Country(AdoptedBase):
        __tablename__ = "countries"
        code: Mapped[str] = mapped_column(String(2), primary_key=True)
        name: Mapped[str] = mapped_column(String(50))

    dto = sqlalchemy_2_dto(Country)
    assert dto.id_field_name == "code"
    assert dto.get_fields()["code"].in_create_request is True
    assert dto.get_fields()["code"].in_update_request is False


def test_an_explicit_dto_field_in_column_info_wins():
    class WithOverride(AdoptedBase):
        __tablename__ = "with_override"
        id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
        secret: Mapped[str] = mapped_column(
            String(50),
            info={
                "dto_field": DtoField(
                    in_read_response=False,
                    in_update_response=False,
                    in_search_response=False,
                    in_update_request=False,
                )
            },
        )

    dto = sqlalchemy_2_dto(WithOverride)
    field = dto.get_fields()["secret"]
    assert field.in_read_response is False
    assert field.in_search_response is False
    # The create response still reveals it (a one-time-reveal field).
    assert field.in_create_response is True
    assert "secret" not in dto.get_rest_models().read_response.model_fields


def test_relationships_are_not_projected():
    from sqlalchemy import ForeignKey
    from sqlalchemy.orm import relationship

    class Parent(AdoptedBase):
        __tablename__ = "parents"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        children: Mapped[list[Child]] = relationship(back_populates="parent")

    class Child(AdoptedBase):
        __tablename__ = "children"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey("parents.id"))
        parent: Mapped[Parent] = relationship(back_populates="children")

    dto = sqlalchemy_2_dto(Child)
    # The FK column is a plain int field; the relationship is left out.
    assert list(dto.get_fields()) == ["id", "parent_id"]


def test_enum_column_maps_to_its_python_enum():
    class WithEnum(AdoptedBase):
        __tablename__ = "with_enum"
        id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
        choice: Mapped[Choice] = mapped_column(SqlEnum(Choice))

    dto = sqlalchemy_2_dto(WithEnum)
    assert dto.__dto_fields__["choice"][0] == Choice


# ---------------------------------------------------------------------------
# SqlResource over the model
# ---------------------------------------------------------------------------


def test_resource_serves_the_model_it_is_given():
    resource = SqlResource(Widget, session_factory=_maker())
    assert resource.model is Widget
    assert resource.table is Widget.__table__
    assert resource.metadata is AdoptedBase.metadata


def test_resource_id_column_resolves_the_mapper_attribute():
    resource = SqlResource(Widget, session_factory=_maker())
    assert resource.id_column is resource.table.c["id"]
    assert inspect(resource.model).local_table is resource.table


def test_a_renamed_identifier_attribute_resolves_to_its_column():
    class Renamed(AdoptedBase):
        __tablename__ = "renamed_countries"
        code: Mapped[str] = mapped_column("country_code", String(2), primary_key=True)
        name: Mapped[str] = mapped_column("country_name", String(50))

    resource = SqlResource(Renamed, session_factory=_maker())
    assert resource.id_column is resource.table.c["country_code"]
    assert inspect(resource.model).local_table is resource.table


def test_required_field_is_not_null():
    class Required(AdoptedBase):
        __tablename__ = "required"
        id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
        label: Mapped[str] = mapped_column(String(50))
        note: Mapped[str | None] = mapped_column(String(50), nullable=True)

    resource = SqlResource(Required, session_factory=_maker())
    assert resource.table.c["label"].nullable is False
    assert resource.table.c["note"].nullable is True


# ---------------------------------------------------------------------------
# Round-trip over the model
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def widget_resources() -> AsyncIterator[tuple[SqlResource[Any], AsyncSession]]:
    """A resource over the model plus a session over a fresh in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Widget, session_factory=maker, encryption_service=_encryption())
    async with engine.begin() as conn:
        await conn.run_sync(AdoptedBase.metadata.create_all)
    async with maker() as session:
        yield resource, session
    await engine.dispose()


async def test_model_crud_round_trip(widget_resources):
    resource, session = widget_resources
    dto = resource.get_dto_type()
    async with resource.get_service({**resource_ctx(session)}) as service:
        created = await service.create(
            dto(name="w", kind=Kind.ALPHA, payload={"x": 1}, nickname=None)
        )
        assert created.id is not None
        assert created.name == "w"
        assert created.kind is Kind.ALPHA
        assert created.payload == {"x": 1}

        fetched = await service.read(created.id)
        assert fetched.name == "w"
        assert fetched.id is fetched.id

        updated = await service.update(dto(id=created.id, name="w2"))
        assert updated.name == "w2"
        assert updated.kind is Kind.ALPHA

        assert await service.count() == 1
        await service.delete(created.id)
        assert await service.count() == 0


def resource_ctx(session: AsyncSession) -> dict[Any, Any]:
    from resourcey.v2.core.service import STORAGE_KEY

    return {STORAGE_KEY: session}


async def test_model_values_bind_to_the_right_columns(widget_resources):
    resource, session = widget_resources
    dto = resource.get_dto_type()
    ref = uuid4()
    async with resource.get_service(resource_ctx(session)) as service:
        await service.create(
            dto(name="w", kind=Kind.BETA, payload={"a": 1}, ref=ref, nickname="nick")
        )
    row = (await session.execute(select(Widget.__table__))).mappings().first()
    assert row is not None
    assert row["name"] == "w"
    assert row["kind"] is Kind.BETA
    assert row["ref"] == ref
    assert row["nickname"] == "nick"


async def test_renamed_identifier_crud():
    class RenamedCrud(AdoptedBase):
        __tablename__ = "renamed_countries_crud"
        code: Mapped[str] = mapped_column("country_code", String(3), primary_key=True)
        name: Mapped[str] = mapped_column("country_name", String(50))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(RenamedCrud, session_factory=maker)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(AdoptedBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with (
            maker() as session,
            resource.get_service(resource_ctx(session)) as service,
        ):
            created = await service.create(dto(code="US", name="United States"))
            assert (created.code, created.name) == ("US", "United States")
            assert (await service.read("US")).name == "United States"
            assert await service.batch_read(["US", "ZZ"]) == [
                created,
                None,
            ]
            page = await service.search(limit=5)
            assert [item.code for item in page.items] == ["US"]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Operation-scoped defaults: timestamps + PATCH semantics
# ---------------------------------------------------------------------------


class Post(AdoptedBase):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ``created_at`` / ``updated_at`` exercise the timestamp convention; the
    # latter is re-set on every update, the former written once.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    # A column carrying an ``onupdate``: the mapping makes it an update default.
    touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.now, onupdate=datetime.now
    )


@pytest_asyncio.fixture
async def post_resources() -> AsyncIterator[tuple[SqlResource[Any], AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Post, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(AdoptedBase.metadata.create_all)
    async with maker() as session:
        yield resource, session
    await engine.dispose()


async def test_omitted_update_field_is_preserved_and_explicit_null_clears_it(post_resources):
    """PATCH semantics: omission leaves the stored value; an explicit null clears it."""
    resource, session = post_resources
    dto = resource.get_dto_type()
    async with resource.get_service(resource_ctx(session)) as service:
        created = await service.create(dto(code="c", description="original"))

        # Omit description -> the stored value must survive.
        updated = await service.update(dto(id=created.id, code="c"))
        assert updated.description == "original"

        # Explicitly null -> cleared.
        cleared = await service.update(dto(id=created.id, description=None))
        assert cleared.description is None


async def test_update_bumps_updated_but_not_created_timestamps(post_resources):
    resource, session = post_resources
    dto = resource.get_dto_type()
    async with resource.get_service(resource_ctx(session)) as service:
        created = await service.create(dto(code="c", description="d"))
        assert created.created_at is not None
        assert created.touched_at is not None

        updated = await service.update(dto(id=created.id, code="c2"))
        # created_at is write-once...
        assert updated.created_at == created.created_at
        # ...while the onupdate timestamp is re-applied.
        assert updated.touched_at >= created.touched_at


async def test_empty_patch_touches_the_row(post_resources):
    """An always-omitted field's update default always fires, so PATCH {} writes."""
    resource, session = post_resources
    dto = resource.get_dto_type()
    async with resource.get_service(resource_ctx(session)) as service:
        created = await service.create(dto(code="c"))
        touched = await service.update(dto(id=created.id))
        assert touched.code == "c"
        assert touched.touched_at >= created.touched_at


async def test_create_defaults_fill_omitted_fields(post_resources):
    resource, session = post_resources
    dto = resource.get_dto_type()
    async with resource.get_service(resource_ctx(session)) as service:
        created = await service.create(dto(code="c"))
        # description is nullable with no client default: the DB/ORM default
        # (None) applies; the create-default factory fills the timestamps.
        assert created.description is None
        assert created.created_at is not None


async def test_app_generated_identifier_factory_is_honoured():
    """A column with a client-side default factory supplies its own value on insert."""
    from sqlalchemy import Uuid

    class WithGeneratedId(AdoptedBase):
        __tablename__ = "with_generated_id"
        id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
        name: Mapped[str] = mapped_column(String(50))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(WithGeneratedId, session_factory=maker)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(AdoptedBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with maker() as session, resource.get_service(resource_ctx(session)) as service:
            generated = uuid4()
            created = await service.create(dto(id=generated, name="n"))
            assert created.id == generated

            # An app-declared id factory is honoured even when nothing is supplied
            # (the unconditional id-skip was removed); the DB is never asked.
            auto = await service.create(dto(name="auto"))
            assert isinstance(auto.id, UUID)
    finally:
        await engine.dispose()


async def test_db_generated_identifier_is_never_passed_on_insert():
    """A server-generated key (no create default) lets the database supply it."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Widget, session_factory=maker)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(AdoptedBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with maker() as session, resource.get_service(resource_ctx(session)) as service:
            created = await service.create(dto(name="w", kind=Kind.ALPHA, payload={}))
            assert created.id is not None
            # source has a server_default and no create default: the default fires.
            assert created.source == "app"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Generated logical defaults (issue #94)
# ---------------------------------------------------------------------------


class UuidNode(AdoptedBase):
    """A UUID primary key with no column-level default: the convention generates it."""

    __tablename__ = "uuid_nodes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    label: Mapped[str] = mapped_column(String(50))


def test_uuid_primary_key_gets_a_generated_factory():
    field = sqlalchemy_2_dto(UuidNode).get_fields()["id"]
    assert field.default_factory_for_create is uuid4
    assert field.in_create_request is False


def test_uuid_column_default_wins_over_the_convention():
    """An explicit ``mapped_column(default=...)`` takes precedence (expressed intent)."""

    def custom_id() -> UUID:
        return UUID("00000000-0000-0000-0000-000000000001")

    class WithIdDefault(AdoptedBase):
        __tablename__ = "uuid_id_defaults"
        id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=custom_id)
        label: Mapped[str] = mapped_column(String(50))

    field = sqlalchemy_2_dto(WithIdDefault).get_fields()["id"]
    # The column's own factory is used, not the convention's bare ``uuid4``.
    assert field.default_factory_for_create is custom_id
    assert field.default_factory_for_create is not uuid4
    assert field.in_create_request is False


def test_uuid_server_default_wins_over_the_convention():
    class WithServerDefault(AdoptedBase):
        __tablename__ = "uuid_server_defaults"
        id: Mapped[UUID] = mapped_column(
            Uuid, primary_key=True, server_default=text("gen_random_uuid()")
        )
        label: Mapped[str] = mapped_column(String(50))

    field = sqlalchemy_2_dto(WithServerDefault).get_fields()["id"]
    # The database supplies the id: no application factory is generated.
    assert field.has_default_for("create") is False
    assert field.in_create_request is False


def test_explicit_dto_field_on_the_primary_key_wins_over_conventions():
    class ExplicitKey(AdoptedBase):
        __tablename__ = "explicit_keys"
        id: Mapped[UUID] = mapped_column(
            Uuid, primary_key=True, info={"dto_field": DtoField(in_create_request=True)}
        )
        label: Mapped[str] = mapped_column(String(50))

    field = sqlalchemy_2_dto(ExplicitKey).get_fields()["id"]
    assert field.in_create_request is True
    assert field.has_default_for("create") is False


def test_uuid_natural_key_stays_client_supplied():
    class Country(AdoptedBase):
        __tablename__ = "uuid_countries"
        code: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
        name: Mapped[str] = mapped_column(String(50))

    dto = sqlalchemy_2_dto(Country)
    field = dto.get_fields()["code"]
    assert dto.id_field_name == "code"
    assert field.in_create_request is True
    assert field.has_default_for("create") is False


async def test_generated_uuid_id_is_used_on_insert():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(UuidNode, session_factory=maker)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(AdoptedBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with maker() as session, resource.get_service(resource_ctx(session)) as service:
            created = await service.create(dto(label="n"))
            assert isinstance(created.id, UUID)
    finally:
        await engine.dispose()
