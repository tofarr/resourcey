"""``BaseResource`` and the resourcey async SQLAlchemy declarative base.

A resource is the central unit of resourcey. ``BaseResource`` extends a
Pydantic ``BaseModel`` and is designed to be subclassed: a single resource
declaration drives the Pydantic create / read / update models and the
SQLAlchemy ORM model. Every generation step is a single-purpose, overridable
hook so a subtype can replace any piece without touching the rest
(progressive enhancement / escape hatches).

Generated models and resolved values are cached on the class so repeated calls
are cheap. The caches are non-annotated class attributes (so Pydantic does
not treat them as model fields) and are stored per-subclass.
"""

from __future__ import annotations

import enum
import types
from datetime import date, datetime, time
from functools import reduce
from typing import Annotated, Any, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, create_model, field_serializer, field_validator
from pydantic.fields import FieldInfo
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Table,
    Time,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, registry

from resourcey.resource.config import ResourceyConfig
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.missing import MISSING
from resourcey.util.naming import camel_to_kebab, camel_to_snake, pluralize
from resourcey.util.search_filter import SearchFilter
from resourcey.util.secret_serialization import dump_secret_str, load_secret_str


class ResourceyBase(DeclarativeBase):
    """resourcey-specific async SQLAlchemy declarative base.

    Generated ORM models extend this. Using a dedicated base (rather than a
    caller's own ``DeclarativeBase``) keeps generated tables in a single
    registry owned by the framework while remaining fully compatible with the
    async SQLAlchemy 2 ORM. Callers can still bring their own declarative base
    — this one is only used for generated models.
    """

    registry = registry()


# Default scalar type -> SQLAlchemy column type mapping used by
# ``get_column_for_field``. Any type not present here raises
# ``ResourceyConfigError`` so the developer supplies an explicit
# ``ResourceyConfig.column``.
#
# ``SecretStr`` maps to ``String``: a sensitive field stores JWE ciphertext
# (variable length, no fixed-length assumption) at rest. Encryption /
# decryption happens at the storage boundary via the secret-serialization
# convention wired onto the generated Pydantic models.
_SCALAR_COLUMN_TYPES: dict[Any, Any] = {
    str: String,
    SecretStr: String,
    int: Integer,
    bool: Boolean,
    float: Float,
    bytes: LargeBinary,
    datetime: DateTime,
    date: Date,
    time: Time,
    UUID: Uuid,
    dict: JSON,
    list: JSON,
}


class BaseResource(BaseModel):
    """Base class for resource declarations.

    Subclass it and declare Pydantic fields; the framework derives the create
    / read / update models and the SQLAlchemy ORM model from that single
    declaration. Each public generation method is an overridable hook.
    """

    # Per-subclass caches. Non-annotated so Pydantic ignores them; stored on
    # each subclass's own ``__dict__`` so subclasses don't share caches.
    _id_field: str
    _create_model: type[BaseModel]
    _read_model: type[BaseModel]
    _update_model: type[BaseModel]
    _sqlalchemy_model: Any

    # ------------------------------------------------------------------
    # Field config resolution
    # ------------------------------------------------------------------

    @classmethod
    def get_config_for_field(cls, field_name: str, field: FieldInfo) -> ResourceyConfig:
        """Return the ``ResourceyConfig`` for a field.

        Reads it from the field metadata if an explicit ``ResourceyConfig`` is
        attached via ``Annotated``; otherwise builds a default config. Then
        applies the id / timestamp / secret conventions.

        The ``SecretStr`` default-off convention (``sortable`` -> ``False``)
        is applied only when no explicit ``ResourceyConfig`` is attached: a
        developer who wants a secret sortable must attach an explicit
        ``ResourceyConfig(sortable=True)`` -- a deliberate, visible opt-out
        of the safe default. (Filtering is gated per-resource by a declared
        search filter class, not by a per-field flag; see
        :meth:`get_search_filter_type`.)
        """
        explicit = False
        for meta in field.metadata:
            if isinstance(meta, ResourceyConfig):
                config = meta
                explicit = True
                break
        else:
            config = ResourceyConfig()

        if field_name == "id":
            # The DB / resource owns the id; REST methods pass the id separately
            # from the model body, so it is neither creatable nor updatable.
            config = config.model_copy(update={"creatable": False, "updatable": False})
        elif field_name in ("created_at", "updated_at"):
            if field.default_factory is not None:
                config = config.model_copy(update={"creatable": False, "updatable": False})
            else:
                raise ResourceyConfigError(
                    f"Field '{field_name}' on {cls.__name__} is a timestamp and must declare a "
                    "default_factory (e.g. default_factory=datetime.utcnow). A fixed default or no "
                    "default is ambiguous; supply an explicit ResourceyConfig override if intended."
                )
        # SecretStr fields are not sortable by default: allowing `sort=field`
        # against a secret lets a client infer the relative ordering of secret
        # values even when the field is excluded from the read model. Skipped
        # when an explicit ResourceyConfig is attached so a developer can
        # deliberately opt a secret into sortability.
        if not explicit and _resolve_scalar_type(field.annotation) is SecretStr:
            config = config.model_copy(update={"sortable": False})
        return config

    @classmethod
    def get_search_filter_type(cls) -> type[SearchFilter[Any]] | None:
        """Return the declared search filter class for this resource, or ``None``.

        When ``None`` (the default) the ``search`` action exposes **no**
        filtering: any ``field__op=value`` query parameter is rejected with
        ``400 invalid_input``; the endpoint serves pure pagination + sort
        only. Filtering is opt-in per resource: a developer declares a
        :class:`~resourcey.util.search_filter.SearchFilter` subclass (typically
        a ``BaseSearchFilter[<SqlAlchemyModel>]`` whose ``<attr>__<op>`` fields
        name exactly the filterable fields/operators) and returns its type
        here. The declared class is the single source of truth for what is
        filterable -- it doubles as the validation schema for the incoming
        query params and supplies the SQL ``WHERE`` via ``filter_sql``.

        Overridable.
        """
        return None

    @classmethod
    def get_id_field(cls) -> str:
        """Return the name of the primary identifier field (default: ``id``).

        Cached on the class. Raises ``ResourceyConfigError`` if no ``id``
        field exists.
        """
        cached = cls.__dict__.get("_id_field")
        if cached is not None:
            return cast(str, cached)
        if "id" in cls.model_fields:
            cls._id_field = "id"
            return "id"
        raise ResourceyConfigError(
            f"Resource {cls.__name__} has no 'id' field; override get_id_field() to specify one."
        )

    # ------------------------------------------------------------------
    # Pydantic model generation
    # ------------------------------------------------------------------

    @classmethod
    def get_create_model(cls) -> type[BaseModel]:
        """Build (and cache) the create model: only ``creatable`` fields.

        Required fields (no original default) stay required. Optional fields
        get ``Field(default=MISSING, validate_default=False)`` so the service
        can tell whether a value was explicitly supplied. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_create_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
            if not config.creatable:
                continue
            annotation = _strip_config(field.annotation)
            if field.is_required():
                fields[name] = (annotation, _clean_field(field))
            else:
                fields[name] = (annotation, _missing_field())
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{cls.__name__}Create",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._create_model = model
        return model

    @classmethod
    def get_read_model(cls) -> type[BaseModel]:
        """Build (and cache) the read model: only ``readable`` fields.

        Secret-bearing (``SecretStr``) fields gain the ``dump_secret_str``
        serializer and ``load_secret_str`` validator so encryption / redaction
        happens at the storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_read_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
            if not config.readable:
                continue
            fields[name] = (_strip_config(field.annotation), _clean_field(field))
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{cls.__name__}Read",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._read_model = model
        return model

    @classmethod
    def get_update_model(cls) -> type[BaseModel]:
        """Build (and cache) the PATCH-style update model: only ``updatable`` fields.

        Every field is optional: each gets
        ``Field(default=MISSING, validate_default=False)`` (original
        default / default_factory dropped) so omitted fields are
        distinguishable from explicitly-supplied ones. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_update_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
            if not config.updatable:
                continue
            fields[name] = (_strip_config(field.annotation), _missing_field())
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{cls.__name__}Update",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._update_model = model
        return model

    # ------------------------------------------------------------------
    # SQLAlchemy model generation
    # ------------------------------------------------------------------

    @classmethod
    def get_table_name(cls) -> str:
        """Derive the SQL table name from the class name.

        Snake-case the class name (``UserRole`` -> ``user_role``), then
        pluralize — appending ``"s"`` or ``"es"`` per the common endings
        (``s`` / ``x`` / ``z`` / ``ch`` / ``sh``) — and lowercase. Irregular
        plurals are left to an override. Overridable.
        """
        return pluralize(camel_to_snake(cls.__name__)).lower()

    @classmethod
    def get_resource_path(cls) -> str:
        """Derive the plural, lower-case, kebab-case REST path segment.

        Independent of :meth:`get_table_name` so an override of one never
        silently changes the other: the SQL table name and the URL path are
        separate concerns and may legitimately diverge. Defaults to the
        plural kebab-case class name (``UserRole`` -> ``user-roles``).
        Overridable.
        """
        return pluralize(camel_to_kebab(cls.__name__)).lower()

    @classmethod
    def get_column_for_field(cls, field_name: str, field: FieldInfo) -> Column[Any]:
        """Generate a SQLAlchemy ``Column`` for a field.

        Honours an explicit ``ResourceyConfig.column`` when provided. Otherwise
        applies the default rules: id -> primary key (int id -> Integer with
        autoincrement), timestamps -> indexed, ``*_id`` -> ambiguous error,
        enums -> String, nested models -> JSON, scalars per the default
        type-mapping table, unmapped types -> ``ResourceyConfigError``.
        """
        config = cls.get_config_for_field(field_name, field)
        if config.column is not None:
            return config.column

        nullable = not field.is_required()

        if field_name == "id":
            py_type = _resolve_scalar_type(field.annotation)
            if py_type is int:
                return Column("id", Integer, primary_key=True, autoincrement=True, nullable=False)
            col_type = _column_type_for(field_name, field.annotation, py_type)
            return Column("id", col_type, primary_key=True, nullable=False)

        if field_name in ("created_at", "updated_at"):
            py_type = _resolve_scalar_type(field.annotation)
            col_type = _column_type_for(field_name, field.annotation, py_type)
            return Column(field_name, col_type, index=True, nullable=nullable)

        if field_name.endswith("_id"):
            raise ResourceyConfigError(
                f"Field '{field_name}' on {cls.__name__} ends in '_id'; the framework cannot infer "
                "its column semantics (foreign key? on-delete behaviour?). Define an explicit "
                "ResourceyConfig(column=Column(...)) for this field."
            )

        py_type = _resolve_scalar_type(field.annotation)
        col_type = _column_type_for(field_name, field.annotation, py_type)
        return Column(field_name, col_type, nullable=nullable)

    @classmethod
    def get_sql_alchemy_model(cls) -> Any:
        """Build (and cache) a SQLAlchemy ORM model from the resource fields.

        Uses ``get_table_name()`` for the table and ``get_column_for_field()``
        for each column. The model extends the resourcey async declarative
        base. Caching is mandatory: the declarative registry keys generated
        classes by name, so regenerating would clash.
        """
        cached = cls.__dict__.get("_sqlalchemy_model")
        if cached is not None:
            return cached
        table_name = cls.get_table_name()
        id_field = cls.get_id_field()
        columns: list[Column[Any]] = []
        for name, field in cls.model_fields.items():
            columns.append(cls.get_column_for_field(name, field))
        table = ResourceyBase.metadata.tables.get(table_name)
        if table is None:
            table = Table(table_name, ResourceyBase.metadata, *columns)
        model = type(
            cls.__name__,
            (ResourceyBase,),
            {"__table__": table, "__mapper_args__": {"primary_key": [table.c[id_field]]}},
        )
        cls._sqlalchemy_model = model
        return model


def _missing_field() -> Any:
    """A field defaulting to ``MISSING`` without validating the sentinel."""
    return Field(default=MISSING, validate_default=False)


def _secret_base(secret_names: set[str]) -> type[BaseModel]:
    """Build a transient base class carrying secret serializers / validators.

    For each name in ``secret_names`` a ``field_serializer`` (dump via
    :func:`dump_secret_str`) and a ``before`` ``field_validator`` (load via
    :func:`load_secret_str`, rewrapped in :class:`SecretStr`) are attached.
    ``check_fields=False`` lets the decorators be defined on the base class
    before the fields exist (they are added by ``create_model``). The base
    class is fresh per call so distinct generated models never share decorator
    bindings. With no secret fields the plain :class:`BaseModel` is returned,
    preserving the #1 generation contract.
    """
    if not secret_names:
        return BaseModel
    namespace: dict[str, Any] = {}
    for name in secret_names:
        namespace[f"_serialize_secret_{name}"] = field_serializer(name, check_fields=False)(
            _make_secret_serializer(name)
        )
        namespace[f"_validate_secret_{name}"] = field_validator(
            name, mode="before", check_fields=False
        )(_make_secret_validator(name))
    return type("_SecretFieldsBase", (BaseModel,), namespace)


def _make_secret_serializer(name: str) -> Any:
    """Return a ``field_serializer`` function bound to ``name`` (closure per field)."""

    def _serialize(self: Any, value: Any, info: Any) -> str:
        if value is None or value is MISSING:
            return value  # type: ignore[return-value]
        if isinstance(value, SecretStr):
            return dump_secret_str(value, info)
        return dump_secret_str(SecretStr(str(value)), info)

    _serialize.__name__ = f"_serialize_secret_{name}"
    return _serialize


def _make_secret_validator(name: str) -> Any:
    """Return a ``field_validator`` function bound to ``name`` (closure per field)."""

    def _validate(value: Any, info: Any) -> Any:
        if value is None or value is MISSING:
            return value
        if isinstance(value, SecretStr):
            return value
        return SecretStr(load_secret_str(str(value), info))

    _validate.__name__ = f"_validate_secret_{name}"
    return _validate


def _clean_field(field: FieldInfo) -> FieldInfo:
    """Return a copy of ``field`` with ``ResourceyConfig`` metadata removed.

    ``ResourceyConfig`` carries a SQLAlchemy ``Column`` that is not
    JSON-schema serializable, so it must not appear on fields that are
    carried verbatim into generated models (e.g. required create-model
    fields and all read-model fields).
    """
    cleaned_meta = [m for m in field.metadata if not isinstance(m, ResourceyConfig)]
    if len(cleaned_meta) == len(field.metadata):
        return field
    new_field = field._copy()
    new_field.metadata = cleaned_meta
    return new_field


def _strip_config(annotation: Any) -> Any:
    """Remove ``ResourceyConfig`` (and other non-type) metadata from an annotation.

    ``ResourceyConfig`` carries a SQLAlchemy ``Column`` which is not
    JSON-schema serializable, so it must not leak into generated Pydantic
    models. For ``Annotated[T, ...]`` this returns the bare ``T``; unions
    are rebuilt with stripped members (preserving the ``None`` branch).
    """
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = get_args(annotation)
    if origin is Annotated:
        return _strip_config(args[0])
    stripped = [_strip_config(a) for a in args]
    # ``types.UnionType`` (``X | Y``) cannot be subscripted; rebuild the
    # union with the ``|`` operator so the result is always a valid type.
    if origin is types.UnionType:
        return reduce(lambda x, y: x | y, stripped)
    return origin[tuple(stripped)]


def _resolve_scalar_type(annotation: Any) -> Any:
    """Reduce a (possibly Optional/Union) annotation to its concrete scalar type."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return _resolve_scalar_type(args[0])
    # For multi-typed unions, prefer the first non-None arg we can map.
    return _resolve_scalar_type(args[0]) if args else annotation


def _column_type_for(field_name: str, annotation: Any, py_type: Any) -> Any:
    """Map a Python type to a SQLAlchemy column type, applying the rules."""
    if py_type is None:
        raise ResourceyConfigError(
            f"Cannot resolve a column type for field '{field_name}' (annotation {annotation})."
        )
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        return String
    if isinstance(py_type, type) and issubclass(py_type, BaseModel):
        return JSON
    col_type = _SCALAR_COLUMN_TYPES.get(py_type)
    if col_type is None:
        raise ResourceyConfigError(
            f"No default SQLAlchemy column type for field '{field_name}' of type {py_type}. "
            "Supply an explicit ResourceyConfig(column=Column(...)) for this field."
        )
    return col_type
