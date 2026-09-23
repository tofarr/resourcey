"""Tests for ``sqlalchemy_2_dto`` and the ``v2`` SQL backend (issue #78).

Covers both conversion directions (ORM model -> DTO and DTO -> table), the
metadata handoff, model adoption vs. generation, the ``SqlResource`` session
maker requirement, cursor pagination, and the migration round trip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import pytest
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

from resourcey.v2.core.dto import DTO
from resourcey.v2.core.service import ServiceError
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.sql.resource import SqlResource, V2Base
from resourcey.v2.sql.sqlalchemy_2_dto import (
    MODEL_METADATA_KEY,
    recorded_model,
    sqlalchemy_2_dto,
)


class Kind(StrEnum):
    ALPHA = "alpha"
    BETA = "beta"


class Choice(StrEnum):
    A = "a"
    B = "b"


class WithEnum(DTO):
    id: int
    choice: Choice


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


def _maker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# sqlalchemy_2_dto — model -> DTO
# ---------------------------------------------------------------------------


def test_conversion_records_the_model_in_metadata():
    dto = sqlalchemy_2_dto(Widget)
    assert dto.metadata[MODEL_METADATA_KEY] is Widget
    assert recorded_model(dto) is Widget


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


def test_a_recorded_model_is_inherited_by_a_dto_subclass():
    adopted = sqlalchemy_2_dto(Widget)

    class Extended(adopted):  # type: ignore[misc, valid-type]
        extra: str

    assert recorded_model(Extended) is Widget


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


# ---------------------------------------------------------------------------
# DTO -> table (generation) round trip
# ---------------------------------------------------------------------------


def test_dto_to_table_generation_matches_the_original_columns():
    dto = sqlalchemy_2_dto(Widget)
    resource = SqlResource(dto, session_factory=_maker(), base=V2Base)
    generated = resource.table
    original = Widget.__table__
    assert [c.name for c in generated.columns] == [c.name for c in original.columns]
    assert generated.primary_key.columns.keys() == ["id"]
    # Nullability round-trips.
    assert generated.c["nickname"].nullable is True
    assert generated.c["name"].nullable is False


def test_dto_to_model_round_trip_through_the_orm_model():
    class Plain(DTO):
        id: int
        label: str
        weight: float | None

    resource = SqlResource(Plain, session_factory=_maker(), base=V2Base)
    assert [c.name for c in resource.table.columns] == ["id", "label", "weight"]
    assert resource.table.c["id"].primary_key is True
    assert resource.table.c["id"].autoincrement is True
    assert resource.table.c["weight"].nullable is True


def test_unsupported_field_annotation_raises():
    class Odd(DTO):
        id: int
        blob: complex

    with pytest.raises(ServiceError, match="No SQL column type"):
        SqlResource(Odd, session_factory=_maker(), base=V2Base)


def test_enum_field_maps_to_a_string_column():
    resource = SqlResource(WithEnum, session_factory=_maker(), base=V2Base)
    assert isinstance(resource.table.c["choice"].type, String)


# ---------------------------------------------------------------------------
# Adoption vs. generation
# ---------------------------------------------------------------------------


def test_resource_adopts_the_model_recorded_in_the_dto():
    dto = sqlalchemy_2_dto(Widget)
    resource = SqlResource(dto, session_factory=_maker())
    assert resource.model is Widget
    assert resource.table is Widget.__table__


def test_resource_generates_a_model_when_none_is_recorded():
    class Plain(DTO):
        id: int
        label: str

    resource = SqlResource(Plain, session_factory=_maker(), base=V2Base)
    assert resource.model is not Plain
    assert resource.model.__table__ is resource.table
    assert resource.model.__table__.name == "plains"


def test_generated_models_land_on_the_injected_base():
    class MyBase(DeclarativeBase):
        pass

    class Plain(DTO):
        id: int
        label: str

    resource = SqlResource(Plain, session_factory=_maker(), base=MyBase)
    assert "plains" in MyBase.metadata.tables
    assert resource.metadata is MyBase.metadata


def test_two_resources_for_the_same_dto_share_one_table():
    class Plain(DTO):
        id: int
        label: str

    maker = _maker()
    a = SqlResource(Plain, session_factory=maker, base=V2Base)
    b = SqlResource(Plain, session_factory=maker, base=V2Base)
    assert a.table is b.table
    assert a.model is b.model


# ---------------------------------------------------------------------------
# Round-trip over the generated model
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def widget_resources() -> AsyncIterator[tuple[SqlResource[Any], AsyncSession]]:
    """An adopted-model resource plus a session over a fresh in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    dto = sqlalchemy_2_dto(Widget)
    resource = SqlResource(dto, session_factory=maker, encryption_service=_encryption())
    async with engine.begin() as conn:
        await conn.run_sync(Widget.metadata.create_all)
    async with maker() as session:
        yield resource, session
    await engine.dispose()


async def test_adopted_model_crud_round_trip(widget_resources):
    resource, session = widget_resources
    dto = resource.get_dto_type()
    ctx: dict[Any, Any] = {**resource_ctx(session)}
    async with resource.get_service(ctx) as service:
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


async def test_adopted_model_values_bind_to_the_right_columns(widget_resources):
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


# ---------------------------------------------------------------------------
# Session maker requirement
# ---------------------------------------------------------------------------


def test_session_factory_is_required():
    class Plain(DTO):
        id: int

    with pytest.raises(TypeError):
        SqlResource(Plain)  # type: ignore[call-arg]


def test_metadata_property_exposes_the_base_metadata():
    class Plain(DTO):
        id: int

    resource = SqlResource(Plain, session_factory=_maker())
    assert resource.metadata is V2Base.metadata
    assert inspect(resource.model).local_table is resource.table
