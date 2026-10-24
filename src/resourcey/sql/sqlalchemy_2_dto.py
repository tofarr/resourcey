"""Infer a DTO declaration from a SQLAlchemy ORM model (issue #78/#89).

:func:`sqlalchemy_2_dto` maps each mapped column back to the corresponding
Python annotation, makes the primary key the DTO identifier (``id_field_name``),
widens nullability to ``ann | None``, and re-expresses client-side defaults as
operation-scoped defaults.

A column may override the inferred projection by placing a
:class:`~resourcey.core.dto.DtoField` in its ``info`` mapping under the
``dto_field`` key::

    key: Mapped[str] = mapped_column(
        String, info={"dto_field": DtoField(in_read_response=False)}
    )

Otherwise the projection is inferred from the column's generation behaviour (see
:func:`_dto_field_for_column`). A foreign-key column also gets
``DtoField.references`` set to the singularised target table name
(``thread_id`` -> ``"thread"``), unless the field already declares one.

**Scope: plain columns only.** A foreign-key column projects as an ordinary
scalar field. SQLAlchemy ``relationship()``s are **not** projected — a documented
limitation.

This module imports no code outside the framework.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, cast, get_args, get_origin, get_type_hints
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Time,
    Uuid,
    inspect,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import Mapper

from resourcey.core.dto import _UNSET, DTO, DtoField, _apply_conventions
from resourcey.sql.model_type import ModelType
from resourcey.util.naming import singularise

# The ``Column.info`` key under which a developer supplies an explicit
# ``DtoField`` for a column; absent it, the projection is inferred.
DTO_FIELD_INFO_KEY = "dto_field"


def sqlalchemy_2_dto(model: type[Any], *, name: str | None = None) -> type[DTO]:
    """Infer a DTO declaration from an ORM model.

    Args:
        model: A mapped SQLAlchemy declarative class.
        name: The DTO class name (defaults to the model's name).
    """
    mapper = inspect(model)
    fields = _field_declarations(mapper)
    namespace: dict[str, Any] = {
        "__annotations__": {field_name: ann for field_name, (ann, _cfg) in fields.items()},
        "__module__": getattr(model, "__module__", __name__),
        **{field_name: cfg for field_name, (_ann, cfg) in fields.items()},
    }
    return type(
        name or model.__name__,
        (DTO,),
        namespace,
        id_field_name=_primary_key_attr(mapper),
    )


# ---------------------------------------------------------------------------
# Column -> DTO field
# ---------------------------------------------------------------------------


def _field_declarations(mapper: Mapper[Any]) -> dict[str, tuple[Any, DtoField]]:
    """The ``{field_name: (annotation, DtoField)}`` declarations for a mapper.

    The column's own generation behaviour wins over a convention-generated
    default.
    """
    declarations: dict[str, tuple[Any, DtoField]] = {}
    id_field_name = _primary_key_attr(mapper)
    hints = _resolved_hints(mapper)
    for attr in mapper.column_attrs:
        column = attr.columns[0]
        annotation = _annotation_for_column(column, hints.get(attr.key))
        explicit = column.info.get(DTO_FIELD_INFO_KEY)
        if isinstance(explicit, DtoField):
            config = explicit
            is_explicit = True
        else:
            config = _dto_field_for_column(column, mapper)
            is_explicit = False
        target = _fk_target_table(column)
        if target is not None and config.references is _UNSET:
            config = config.with_overrides(references=singularise(target))
        declarations[attr.key] = (
            annotation,
            _apply_conventions(
                attr.key,
                id_field_name,
                annotation,
                config,
                is_explicit,
                identifier_is_backend_managed=_column_generates_id(column, mapper),
            ),
        )
    return declarations


def _column_generates_id(column: Any, mapper: Mapper[Any]) -> bool:
    """Whether the database, not the application, supplies this column's value."""
    return column.server_default is not None or _is_auto_increment(column, mapper)


def _fk_target_table(column: Any) -> str | None:
    """The referenced table's name for ``column``'s first foreign key, if any.

    ``target_fullname`` is ``"<table>.<column>"`` (or
    ``"<schema>.<table>.<column>"``), so the table is the second-to-last part.
    """
    fk = next(iter(column.foreign_keys), None)
    if fk is None:
        return None
    parts = fk.target_fullname.split(".")
    if len(parts) < 2:
        return parts[0] if parts else None
    return cast(str, parts[-2])


def _dto_field_for_column(column: Any, mapper: Mapper[Any]) -> DtoField:
    """The ``DtoField`` inferred from a column's type and generation behaviour.

    A client-side ``default`` becomes the create default (and drops the field
    from create requests); a client-side ``onupdate`` becomes the update default.
    A server-side default or auto-increment key drops the field from create with
    no default; a nullable column with no default gets ``default_for_create=None``.
    """
    create_overrides = _default_overrides(column.default, "create")
    update_overrides = _default_overrides(column.onupdate, "update")
    if create_overrides:
        # A create default means the application supplies the value, so the field
        # is not client-suppliable (though it is still filled on create).
        return DtoField(in_create_request=False, **create_overrides, **update_overrides)
    if _column_generates_id(column, mapper):
        # The database supplies the value: drop from create with no default.
        return DtoField(in_create_request=False, **update_overrides)
    if column.nullable:
        # Optionality comes only from a declared default, so a nullable column
        # with none keeps ORM ergonomics via an explicit ``None`` default.
        return DtoField(default_for_create=None, **update_overrides)
    return DtoField(**update_overrides)


def _default_overrides(default: Any, operation: str) -> dict[str, Any]:
    """Map a SQLAlchemy default to a ``default_for_*`` / ``default_factory_for_*`` pair."""
    if default is None:
        return {}
    value_key = f"default_for_{operation}"
    factory_key = f"default_factory_for_{operation}"
    if getattr(default, "is_callable", False):
        # SQLAlchemy wraps a callable default in an ``(ctx)``-taking adapter;
        # unwrap to the author's callable so the DTO factory is arity-correct.
        factory = getattr(default.arg, "__wrapped__", default.arg)
        return {factory_key: factory}
    return {value_key: default.arg}


def _is_auto_increment(column: Any, mapper: Mapper[Any]) -> bool:
    """Whether a primary-key column's value is generated by the database."""
    if not column.primary_key:
        return False
    if column.autoincrement is True:
        return True
    # A dataclass-configured model marks a server-generated key ``init=False``.
    attr = mapper.get_property_by_column(column)
    dataclass_fields = getattr(mapper.class_, "__dataclass_fields__", {})
    return getattr(dataclass_fields.get(attr.key), "init", True) is False


def _primary_key_attr(mapper: Mapper[Any]) -> str:
    """The mapped attribute name of the first primary-key column."""
    return str(mapper.get_property_by_column(mapper.primary_key[0]).key)


def _annotation_for_column(column: Any, declared: Any = None) -> Any:
    """The Python annotation corresponding to a column (nullable widens it).

    The column *type* drives the annotation, except for a ``SecretStr`` column,
    whose Python type carries behaviour the SQL type cannot express.
    """
    if _is_secret_column(declared):
        annotation: Any = SecretStr
    elif isinstance(column.type, ModelType):
        # SQLAlchemy erases the type parameter to ``Any``, so the bound model is
        # recovered from the column type itself.
        annotation = _model_type_of(column) or Any
    elif isinstance(column.type, JSON):
        # The SQL type says only "JSON"; the declared annotation carries the
        # container shape, so prefer it for a list / dict column.
        declared_container = _declared_annotation(declared)
        origin = get_origin(declared_container)
        annotation = declared_container if origin in (list, dict) else dict
    else:
        annotation = _scalar_annotation(column.type)
    return annotation | None if column.nullable else annotation


def _model_type_of(column: Any) -> Any:
    """The Pydantic model a :class:`~resourcey.sql.model_type.ModelType` column binds.

    Read from the column-type instance, which survives a SQLAlchemy ``copy``.
    """
    return getattr(column.type, "model", None)


def _declared_annotation(declared: Any) -> Any:
    """Reduce ``Mapped[T]`` / ``T | None`` to ``T`` for a container annotation.

    Keeps the *whole* container type, so a ``Mapped[list[str]]`` JSON column
    projects as ``list[str]`` rather than ``dict``.
    """
    if declared is None:
        return None
    origin = get_origin(declared)
    if origin is not None and getattr(origin, "__name__", "") == "Mapped":
        args = get_args(declared)
        return _declared_annotation(args[0]) if args else None
    if type(None) in get_args(declared):
        non_none = [arg for arg in get_args(declared) if arg is not type(None)]
        return non_none[0] if len(non_none) == 1 else declared
    return declared


def _resolved_hints(mapper: Mapper[Any]) -> dict[str, Any]:
    """The model's resolved annotations (``include_extras``), or ``{}`` if unresolved.

    A forward reference that cannot be resolved degrades to no hints rather than
    failing the whole inference.
    """
    try:
        return get_type_hints(mapper.class_, include_extras=True)
    except Exception:
        return {}


def _is_secret_column(declared: Any) -> bool:
    """Whether the mapped attribute was declared ``Mapped[SecretStr]``.

    Only the annotation carries that intent (the SQL type is ``String``), so this
    unwraps ``Mapped[...]`` / ``Optional[...]`` and checks for ``SecretStr``.
    """
    return _unwrap_mapped(declared) is SecretStr


def _unwrap_mapped(annotation: Any) -> Any:
    """Reduce a ``Mapped[T]`` annotation to ``T``, following the declared union."""
    if annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is not None and getattr(origin, "__name__", "") == "Mapped":
        args = get_args(annotation)
        return _unwrap_mapped(args[0]) if args else None
    if origin is not None:
        for arg in get_args(annotation):
            if arg is not type(None) and _unwrap_mapped(arg) is SecretStr:
                return SecretStr
    return annotation


def _scalar_annotation(column_type: Any) -> Any:
    """Map a SQLAlchemy column type to a Python annotation.

    Ordered so subclasses resolve to their specific Python type: ``Enum`` is a
    ``String`` subclass and ``Float`` is a ``Numeric`` subclass.
    """
    if isinstance(column_type, SqlEnum):
        return column_type.enum_class if column_type.enum_class is not None else str
    if isinstance(column_type, Boolean):
        return bool
    if isinstance(column_type, Integer):
        return int
    if isinstance(column_type, Float):
        return float
    if isinstance(column_type, Numeric):
        return Decimal
    if isinstance(column_type, DateTime):
        return datetime
    if isinstance(column_type, Date):
        return date
    if isinstance(column_type, Time):
        return time
    if isinstance(column_type, LargeBinary):
        return bytes
    if isinstance(column_type, Uuid):
        return UUID
    if isinstance(column_type, String):
        return str
    return Any
