"""Tests for ``BaseResource`` model + column generation."""

import enum
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService
from resourcey.resource.base import BaseResource, ResourceyBase
from resourcey.resource.config import ResourceyConfig
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.missing import MISSING

# ---------------------------------------------------------------------------
# Resource fixtures
# ---------------------------------------------------------------------------


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Address(BaseModel):
    street: str


class User(BaseResource):
    id: int
    email: str
    name: str | None = None
    age: int = 0
    score: float = 0.0
    active: bool = True
    payload: dict = Field(default_factory=dict)
    tags: list = Field(default_factory=list)
    blob: bytes = b""
    color: Color = Color.RED
    address: Address = Address(street="x")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime | None = Field(default_factory=datetime.utcnow)


class Role(BaseResource):
    id: int
    label: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Widget(BaseResource):
    id: int
    label: str


class Box(BaseResource):
    id: int
    size: int


class UserRole(BaseResource):
    id: int
    label: str


class BankAccount(BaseResource):
    id: int
    balance: int


class HttpServer(BaseResource):
    id: int
    host: str


class WithFk(BaseResource):
    id: int
    role_id: Annotated[
        int, ResourceyConfig(column=Column("role_id", Integer, ForeignKey("roles.id")))
    ]


class NoId(BaseResource):
    email: str


class BadTimestampDefault(BaseResource):
    id: int
    created_at: datetime = datetime(2020, 1, 1)


class BadTimestampNoDefault(BaseResource):
    id: int
    created_at: datetime


class WithAmbiguousId(BaseResource):
    id: int
    owner_id: int


class WithUuidId(BaseResource):
    id: UUID
    name: str


class WithUnmappedType(BaseResource):
    id: int
    thing: complex = complex(0)


class SecretResource(BaseResource):
    id: int
    name: str
    token: SecretStr
    optional_token: SecretStr | None = None


def _encryption_service() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="k1", value=SecretStr("test-secret")),
        )
    )


# ---------------------------------------------------------------------------
# get_id_field
# ---------------------------------------------------------------------------


def test_get_id_field_returns_id():
    assert User.get_id_field() == "id"


def test_get_id_field_cached():
    first = User.get_id_field()
    second = User.get_id_field()
    assert first is second
    assert "_id_field" in User.__dict__


def test_get_id_field_raises_when_no_id():
    with pytest.raises(ResourceyConfigError):
        NoId.get_id_field()


def test_get_id_field_overridable():
    class CustomId(BaseResource):
        email: str

        @classmethod
        def get_id_field(cls) -> str:
            return "email"

    assert CustomId.get_id_field() == "email"


# ---------------------------------------------------------------------------
# get_config_for_field
# ---------------------------------------------------------------------------


def test_config_for_id_field():
    cfg = User.get_config_for_field("id", User.model_fields["id"])
    assert cfg.creatable is False
    assert cfg.updatable is False
    assert cfg.readable is True
    assert cfg.sortable is True


def test_config_for_email_field_defaults():
    cfg = User.get_config_for_field("email", User.model_fields["email"])
    assert cfg.creatable is True
    assert cfg.updatable is True
    assert cfg.readable is True
    assert cfg.sortable is True
    assert cfg.column is None


def test_config_for_timestamp_with_default_factory():
    cfg = User.get_config_for_field("created_at", User.model_fields["created_at"])
    assert cfg.creatable is False
    assert cfg.updatable is False


def test_config_for_timestamp_with_fixed_default_raises():
    with pytest.raises(ResourceyConfigError):
        BadTimestampDefault.get_config_for_field(
            "created_at", BadTimestampDefault.model_fields["created_at"]
        )


def test_config_for_timestamp_with_no_default_raises():
    with pytest.raises(ResourceyConfigError):
        BadTimestampNoDefault.get_config_for_field(
            "created_at", BadTimestampNoDefault.model_fields["created_at"]
        )


def test_config_reads_explicit_annotated_metadata():
    cfg = WithFk.get_config_for_field("role_id", WithFk.model_fields["role_id"])
    assert cfg.column is not None
    # The explicit config is preserved (not reset to defaults by conventions,
    # since role_id is not id / timestamp).
    assert isinstance(cfg.column, Column)


def test_get_config_for_field_overridable():
    class Override(BaseResource):
        id: int
        x: int

        @classmethod
        def get_config_for_field(cls, field_name, field):
            cfg = super().get_config_for_field(field_name, field)
            return cfg.model_copy(update={"creatable": False})

    cfg = Override.get_config_for_field("x", Override.model_fields["x"])
    assert cfg.creatable is False


# ---------------------------------------------------------------------------
# get_config_for_field -- SecretStr sortable default (issue #2 / #7)
# ---------------------------------------------------------------------------


def test_config_for_secret_str_defaults_not_sortable():
    cfg = SecretResource.get_config_for_field("token", SecretResource.model_fields["token"])
    assert cfg.sortable is False
    # Other flags are unaffected -- secrets stay creatable/updatable/readable
    # (readability is governed by redaction/encryption at the storage boundary,
    # not by excluding the field from the read model).
    assert cfg.creatable is True
    assert cfg.updatable is True
    assert cfg.readable is True


def test_config_for_optional_secret_str_defaults_not_sortable():
    cfg = SecretResource.get_config_for_field(
        "optional_token", SecretResource.model_fields["optional_token"]
    )
    assert cfg.sortable is False


def test_config_for_secret_str_explicit_override_is_respected():
    """An explicit ResourceyConfig(sortable=True) on a SecretStr wins over the
    default-off convention -- the override is the documented escape hatch."""

    class SecretWithOverride(BaseResource):
        id: int
        token: Annotated[SecretStr, ResourceyConfig(sortable=True)]

    cfg = SecretWithOverride.get_config_for_field("token", SecretWithOverride.model_fields["token"])
    assert cfg.sortable is True


# ---------------------------------------------------------------------------
# get_search_filter_type (issue #2)
# ---------------------------------------------------------------------------


def test_get_search_filter_type_defaults_to_none():
    """No filter class declared by default -> no filtering is available."""
    assert User.get_search_filter_type() is None
    assert SecretResource.get_search_filter_type() is None


def test_get_search_filter_type_overridable():
    from resourcey.util.search_filter import SearchFilter

    class CustomFilter(SearchFilter):
        pass

    class CustomResource(BaseResource):
        id: int

        @classmethod
        def get_search_filter_type(cls) -> type[SearchFilter[Any]] | None:
            return CustomFilter

    assert CustomResource.get_search_filter_type() is CustomFilter


# ---------------------------------------------------------------------------
# get_create_model
# ---------------------------------------------------------------------------


def test_create_model_excludes_non_creatable_fields():
    create_model = User.get_create_model()
    fields = set(create_model.model_fields)
    assert "id" not in fields
    assert "created_at" not in fields
    assert "updated_at" not in fields
    assert "email" in fields


def test_create_model_required_fields_stay_required():
    create_model = User.get_create_model()
    assert create_model.model_fields["email"].is_required()


def test_create_model_optional_fields_default_to_missing():
    create_model = User.get_create_model()
    name_field = create_model.model_fields["name"]
    assert not name_field.is_required()
    assert name_field.default is MISSING
    assert name_field.validate_default is False


def test_create_model_instance_detects_missing():
    create_model = User.get_create_model()
    instance = create_model(email="a@b.com")
    assert instance.name is MISSING
    assert instance.age is MISSING
    # Explicitly-supplied values are validated and stored.
    assert create_model(email="a@b.com", age=5).age == 5


def test_create_model_cached():
    assert User.get_create_model() is User.get_create_model()
    assert "_create_model" in User.__dict__


def test_create_model_per_subclass():
    assert User.get_create_model() is not Widget.get_create_model()


# ---------------------------------------------------------------------------
# get_read_model
# ---------------------------------------------------------------------------


def test_read_model_contains_readable_fields():
    read_model = User.get_read_model()
    fields = set(read_model.model_fields)
    assert "id" in fields
    assert "email" in fields
    assert "created_at" in fields


def test_read_model_excludes_unreadable_field():
    class Secret(BaseResource):
        id: int
        token: Annotated[str, ResourceyConfig(readable=False)] = "x"

    read_model = Secret.get_read_model()
    assert "token" not in read_model.model_fields
    assert "id" in read_model.model_fields


def test_read_model_cached():
    assert User.get_read_model() is User.get_read_model()
    assert "_read_model" in User.__dict__


# ---------------------------------------------------------------------------
# get_update_model
# ---------------------------------------------------------------------------


def test_update_model_excludes_non_updatable_fields():
    update_model = User.get_update_model()
    fields = set(update_model.model_fields)
    assert "id" not in fields
    assert "created_at" not in fields
    assert "updated_at" not in fields
    assert "email" in fields


def test_update_model_all_fields_optional_with_missing():
    update_model = User.get_update_model()
    for name, field in update_model.model_fields.items():
        assert not field.is_required(), name
        assert field.default is MISSING, name
        assert field.validate_default is False, name


def test_update_model_instance_detects_missing():
    update_model = User.get_update_model()
    instance = update_model()
    assert instance.email is MISSING
    updated = update_model(email="new@x.com")
    assert updated.email == "new@x.com"


def test_update_model_cached():
    assert User.get_update_model() is User.get_update_model()
    assert "_update_model" in User.__dict__


# ---------------------------------------------------------------------------
# get_table_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (User, "users"),
        (Box, "boxes"),
        (Widget, "widgets"),
        (Role, "roles"),
        (UserRole, "user_roles"),
        (BankAccount, "bank_accounts"),
        (HttpServer, "http_servers"),
    ],
)
def test_get_table_name(resource, expected):
    assert resource.get_table_name() == expected


def test_get_table_name_overridable():
    class Custom(BaseResource):
        id: int

        @classmethod
        def get_table_name(cls) -> str:
            return "custom_table"

    assert Custom.get_table_name() == "custom_table"


# ---------------------------------------------------------------------------
# get_resource_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (User, "users"),
        (Box, "boxes"),
        (UserRole, "user_roles"),
        (BankAccount, "bank_accounts"),
        (HttpServer, "http_servers"),
    ],
)
def test_get_resource_path(resource, expected):
    assert resource.get_resource_path() == expected


def test_get_resource_path_independent_of_table_name_override():
    """Overriding get_table_name must not change the default resource path.

    The two naming concerns are independent hooks: a table-name override
    (e.g. to an irregular ``custom_table``) must not leak into the URL path,
    which stays the default plural snake_case class name.
    """

    class Custom(BaseResource):
        id: int

        @classmethod
        def get_table_name(cls) -> str:
            return "custom_table"

    assert Custom.get_table_name() == "custom_table"
    # Not "custom_table" / "custom_tables" -- derived independently from the
    # class name, so the URL path is unaffected by the table-name override.
    assert Custom.get_resource_path() == "customs"


def test_get_resource_path_overridable():
    class Custom(BaseResource):
        id: int

        @classmethod
        def get_resource_path(cls) -> str:
            return "custom-path"

    assert Custom.get_resource_path() == "custom-path"


# ---------------------------------------------------------------------------
# get_column_for_field
# ---------------------------------------------------------------------------


def test_column_for_int_id_is_primary_key_with_autoincrement():
    col = User.get_column_for_field("id", User.model_fields["id"])
    assert col.primary_key is True
    assert col.autoincrement is True
    assert col.nullable is False
    assert isinstance(col.type, Integer)


def test_column_for_created_at_is_indexed():
    col = User.get_column_for_field("created_at", User.model_fields["created_at"])
    assert col.index is True
    assert isinstance(col.type, DateTime)


def test_column_for_string_field():
    col = User.get_column_for_field("email", User.model_fields["email"])
    assert isinstance(col.type, String)
    assert col.nullable is False  # email is required


def test_column_nullable_for_optional_field():
    col = User.get_column_for_field("name", User.model_fields["name"])
    assert col.nullable is True


def test_column_for_enum_is_string():
    col = User.get_column_for_field("color", User.model_fields["color"])
    assert isinstance(col.type, String)


def test_column_for_nested_model_is_json():
    col = User.get_column_for_field("address", User.model_fields["address"])
    assert isinstance(col.type, JSON)


def test_column_for_dict_is_json():
    col = User.get_column_for_field("payload", User.model_fields["payload"])
    assert isinstance(col.type, JSON)


def test_column_for_list_is_json():
    col = User.get_column_for_field("tags", User.model_fields["tags"])
    assert isinstance(col.type, JSON)


def test_column_for_ambiguous_id_raises():
    with pytest.raises(ResourceyConfigError):
        WithAmbiguousId.get_column_for_field("owner_id", WithAmbiguousId.model_fields["owner_id"])


def test_column_honours_explicit_override():
    col = WithFk.get_column_for_field("role_id", WithFk.model_fields["role_id"])
    assert col.name == "role_id"
    assert len(col.foreign_keys) == 1


def test_column_for_uuid_id():
    col = WithUuidId.get_column_for_field("id", WithUuidId.model_fields["id"])
    assert col.primary_key is True
    assert col.nullable is False


def test_column_for_unmapped_type_raises():
    with pytest.raises(ResourceyConfigError):
        WithUnmappedType.get_column_for_field("thing", WithUnmappedType.model_fields["thing"])


def test_get_column_for_field_overridable():
    class Override(BaseResource):
        id: int
        flag: bool

        @classmethod
        def get_column_for_field(cls, field_name, field):
            if field_name == "flag":
                return Column("flag", Integer)
            return super().get_column_for_field(field_name, field)

    col = Override.get_column_for_field("flag", Override.model_fields["flag"])
    assert isinstance(col.type, Integer)


# ---------------------------------------------------------------------------
# get_sql_alchemy_model
# ---------------------------------------------------------------------------


def test_sql_alchemy_model_extends_resourcey_base():
    sqla_model = User.get_sql_alchemy_model()
    assert issubclass(sqla_model, ResourceyBase)


def test_sql_alchemy_model_table_name_and_primary_key():
    sqla_model = User.get_sql_alchemy_model()
    assert sqla_model.__table__.name == "users"
    pk_cols = [c for c in sqla_model.__table__.columns if c.primary_key]
    assert [c.name for c in pk_cols] == ["id"]


def test_sql_alchemy_model_has_all_columns():
    sqla_model = User.get_sql_alchemy_model()
    names = {c.name for c in sqla_model.__table__.columns}
    assert names == set(User.model_fields)


def test_sql_alchemy_model_cached():
    assert User.get_sql_alchemy_model() is User.get_sql_alchemy_model()
    assert "_sqlalchemy_model" in User.__dict__


def test_sql_alchemy_model_per_subclass():
    assert User.get_sql_alchemy_model() is not Widget.get_sql_alchemy_model()


def test_sql_alchemy_model_keyed_on_id_field():
    sqla_model = User.get_sql_alchemy_model()
    # primary_key mapper arg points at the id column
    pk_cols = sqla_model.__mapper__.primary_key
    assert [c.name for c in pk_cols] == ["id"]


async def test_sql_alchemy_model_round_trip():
    # Build the ORM model first so its table is registered on the shared
    # metadata before create_all runs on a fresh in-memory engine.
    sqla_model = Widget.get_sql_alchemy_model()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(ResourceyBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as sess:
            w = sqla_model()
            w.id = 1
            w.label = "gadget"
            sess.add(w)
            await sess.commit()
            result = await sess.execute(select(sqla_model))
            rows = result.scalars().all()
            assert [(r.id, r.label) for r in rows] == [(1, "gadget")]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Secret-field (SecretStr) handling — issue #7
# ---------------------------------------------------------------------------


def test_column_for_secret_str_is_string():
    col = SecretResource.get_column_for_field("token", SecretResource.model_fields["token"])
    assert isinstance(col.type, String)
    assert col.nullable is False  # token is required


def test_column_for_optional_secret_str_is_nullable_string():
    col = SecretResource.get_column_for_field(
        "optional_token", SecretResource.model_fields["optional_token"]
    )
    assert isinstance(col.type, String)
    assert col.nullable is True


def test_secret_str_field_no_longer_raises_unmapped_type():
    # Before issue #7 a SecretStr field raised ResourceyConfigError because it
    # was absent from _SCALAR_COLUMN_TYPES. Now it maps to a String column.
    sqla_model = SecretResource.get_sql_alchemy_model()
    names = {c.name for c in sqla_model.__table__.columns}
    assert {"id", "name", "token", "optional_token"} == names


def test_create_model_with_secret_field_redacts_on_default_dump():
    create_model = SecretResource.get_create_model()
    instance = create_model(
        name="svc", token=SecretStr("plain-token"), optional_token=SecretStr("opt")
    )
    dumped = instance.model_dump()
    assert dumped["token"] == "**********"
    assert dumped["optional_token"] == "**********"
    assert dumped["name"] == "svc"


def test_create_model_with_secret_field_redacts_on_json_dump():
    create_model = SecretResource.get_create_model()
    instance = create_model(
        name="svc", token=SecretStr("plain-token"), optional_token=SecretStr("opt")
    )
    assert (
        instance.model_dump_json()
        == '{"name":"svc","token":"**********","optional_token":"**********"}'
    )


def test_create_model_secret_field_encrypts_with_context():
    enc = _encryption_service()
    create_model = SecretResource.get_create_model()
    instance = create_model(
        name="svc", token=SecretStr("plain-token"), optional_token=SecretStr("opt")
    )
    dumped = instance.model_dump(context={"encryption_service": enc})
    assert dumped["token"] != "plain-token"
    assert dumped["token"] != "**********"
    # The ciphertext round-trips through the service.
    assert enc.decrypt_value(dumped["token"]) == "plain-token"


def test_create_model_secret_field_exposes_plaintext_with_context():
    create_model = SecretResource.get_create_model()
    instance = create_model(
        name="svc", token=SecretStr("plain-token"), optional_token=SecretStr("opt")
    )
    dumped = instance.model_dump(context={"expose_secrets": True})
    assert dumped["token"] == "plain-token"
    assert dumped["optional_token"] == "opt"


def test_create_model_non_secret_field_is_not_encrypted():
    enc = _encryption_service()
    create_model = SecretResource.get_create_model()
    instance = create_model(
        name="svc", token=SecretStr("plain-token"), optional_token=SecretStr("opt")
    )
    dumped = instance.model_dump(context={"encryption_service": enc})
    # A plain str field is never touched by the secret convention.
    assert dumped["name"] == "svc"


def test_read_model_secret_field_round_trip_with_encryption_context():
    enc = _encryption_service()
    create_model = SecretResource.get_create_model()
    read_model = SecretResource.get_read_model()
    instance = create_model(
        name="svc", token=SecretStr("round-trip-value"), optional_token=SecretStr("opt")
    )
    dumped = instance.model_dump(context={"encryption_service": enc})
    # Validate the ciphertext back into a read model with the same service.
    loaded = read_model.model_validate(
        {
            "id": 1,
            "name": "svc",
            "token": dumped["token"],
            "optional_token": dumped["optional_token"],
        },
        context={"encryption_service": enc},
    )
    assert isinstance(loaded.token, SecretStr)
    assert loaded.token.get_secret_value() == "round-trip-value"
    assert loaded.optional_token.get_secret_value() == "opt"


def test_read_model_secret_field_passes_through_without_encryption_context():
    read_model = SecretResource.get_read_model()
    # Without a service the stored value is treated as plaintext pass-through.
    loaded = read_model.model_validate(
        {"id": 1, "name": "svc", "token": "raw", "optional_token": "rawopt"},
        context={},
    )
    assert loaded.token.get_secret_value() == "raw"
    assert loaded.optional_token.get_secret_value() == "rawopt"


def test_update_model_secret_field_encrypts_with_context():
    enc = _encryption_service()
    update_model = SecretResource.get_update_model()
    instance = update_model(token=SecretStr("new-token"))
    dumped = instance.model_dump(
        context={"encryption_service": enc}, exclude={"name", "optional_token"}
    )
    assert enc.decrypt_value(dumped["token"]) == "new-token"


def test_update_model_secret_field_missing_when_omitted():
    # An omitted secret field defaults to MISSING (the service distinguishes
    # omitted from explicitly-supplied), matching the non-secret update contract.
    update_model = SecretResource.get_update_model()
    instance = update_model()
    assert instance.token is MISSING


def test_resource_without_secret_fields_uses_plain_basemodel():
    # No secret fields -> the generated model's base is plain BaseModel
    # (the #1 generation contract is preserved).
    create_model = Widget.get_create_model()
    assert create_model.__bases__[0] is BaseModel
    instance = create_model(label="gadget")
    assert instance.model_dump() == {"label": "gadget"}


def test_secret_field_optional_value_encrypts_when_set():
    enc = _encryption_service()
    create_model = SecretResource.get_create_model()
    instance = create_model(name="svc", token=SecretStr("req"), optional_token=SecretStr("opt"))
    dumped = instance.model_dump(context={"encryption_service": enc})
    assert enc.decrypt_value(dumped["optional_token"]) == "opt"


def test_secret_field_validator_accepts_secret_str_input():
    # A SecretStr passed directly to validation is preserved as-is.
    read_model = SecretResource.get_read_model()
    loaded = read_model.model_validate(
        {"id": 1, "name": "svc", "token": SecretStr("direct"), "optional_token": None},
        context={},
    )
    assert loaded.token.get_secret_value() == "direct"
