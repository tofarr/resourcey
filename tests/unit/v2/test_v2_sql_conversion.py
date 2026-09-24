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


def test_client_side_defaults_become_logical_defaults():
    dto = sqlalchemy_2_dto(Widget)
    fields = dto.get_fields()
    assert fields["score"].logical_default_value == 0.0
    assert fields["score"].in_create_request is False
    assert fields["enabled"].in_create_request is False
    # A callable default becomes a logical-default factory (unwrapped from the
    # SQLAlchemy ``(ctx)`` adapter so it takes no arguments).
    assert isinstance(fields["created_at"].logical_default_value_factory(), datetime)
    assert isinstance(fields["ref"].logical_default_value_factory(), UUID)


def test_server_default_drops_from_create_without_a_logical_default():
    dto = sqlalchemy_2_dto(Widget)
    field = dto.get_fields()["source"]
    assert field.in_create_request is False
    assert field.has_logical_default is False


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

        updated = await service.update(created.id, dto(name="w2"))
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
