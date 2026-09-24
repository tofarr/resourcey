"""Infer a ``v2`` DTO declaration from a SQLAlchemy ORM model (issue #78/#89).

This is the new direction of the SQL workflow: a developer defines the ORM
model they already work with and the framework infers the DTO from it.
:func:`sqlalchemy_2_dto` maps each mapped column back to the corresponding
Python annotation, makes the primary key the DTO identifier
(``id_field_name``), widens nullability to ``ann | None``, and re-expresses
client-side defaults as operation-scoped defaults /
``in_create_request=False``.

A column may override the inferred projection by placing a
:class:`~resourcey.v2.core.dto.DtoField` in its ``info`` mapping under the
``dto_field`` key::

    key: Mapped[str] = mapped_column(
        String, info={"dto_field": DtoField(in_read_response=False)}
    )

When no ``dto_field`` is supplied the projection is inferred from the column's
generation behaviour (see :func:`_dto_field_for_column`).

**Scope: plain columns only.** Every column projects as an ordinary scalar
field (a foreign-key column such as ``thread_id`` becomes a plain ``int`` field
and round-trips as a value). SQLAlchemy ``relationship()``s are **not**
projected and are a documented limitation; nested / relationship projection is
a separate future feature.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

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

from resourcey.v2.core.dto import DTO, DtoField

# The ``Column.info`` key under which a developer supplies an explicit
# ``DtoField`` for a column; absent it, the projection is inferred.
DTO_FIELD_INFO_KEY = "dto_field"


def sqlalchemy_2_dto(model: type[Any], *, name: str | None = None) -> type[DTO]:
    """Infer a DTO declaration from an ORM model.

    The DTO's fields mirror the model's mapped columns (attribute order), with
    the primary key as the DTO identifier. A column carrying a ``DtoField`` in
    ``column.info["dto_field"]`` uses it verbatim; otherwise the field is
    inferred from the column type and generation behaviour.

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
    """The ``{field_name: (annotation, DtoField)}`` declarations for a mapper."""
    declarations: dict[str, tuple[Any, DtoField]] = {}
    for attr in mapper.column_attrs:
        column = attr.columns[0]
        declarations[attr.key] = (
            _annotation_for_column(column),
            _dto_field_for_column(column, mapper),
        )
    return declarations


def _dto_field_for_column(column: Any, mapper: Mapper[Any]) -> DtoField:
    """The ``DtoField`` for a column: the explicit one in ``info``, else inferred.

    A client-side ``default`` becomes the create default and the field drops out
    of create requests (the application supplies it); a client-side ``onupdate``
    becomes the update default (so an update omitted by the client re-applies
    it). A server-side default or an auto-increment primary key drops the field
    from create requests *without* a default (the database supplies the value);
    a nullable column with no default gets ``default_for_create=None``, which
    preserves ORM ergonomics after optionality stopped coming from the
    annotation.
    """
    explicit = column.info.get(DTO_FIELD_INFO_KEY)
    if isinstance(explicit, DtoField):
        return explicit
    create_overrides = _default_overrides(column.default, "create")
    update_overrides = _default_overrides(column.onupdate, "update")
    if create_overrides:
        # A create default means the application supplies the value, so the field
        # is not client-suppliable (though it is still filled on create).
        return DtoField(in_create_request=False, **create_overrides, **update_overrides)
    if column.server_default is not None or _is_auto_increment(column, mapper):
        # The database supplies the value: drop from create with no default.
        return DtoField(in_create_request=False, **update_overrides)
    if column.nullable:
        # Optionality no longer comes from the annotation, so a nullable column
        # with no default keeps ORM ergonomics via an explicit ``None`` default.
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


def _annotation_for_column(column: Any) -> Any:
    """The Python annotation corresponding to a column (nullable widens it)."""
    annotation = _scalar_annotation(column.type)
    return annotation | None if column.nullable else annotation


def _scalar_annotation(column_type: Any) -> Any:
    """Map a SQLAlchemy column type to a Python annotation.

    Ordered so subclasses resolve to their specific Python type: ``Enum`` is a
    ``String`` subclass, ``Float`` is a ``Numeric`` subclass, and ``Uuid`` is an
    emulated generic. Unknown types fall back to ``Any``.
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
    if isinstance(column_type, JSON):
        return dict
    if isinstance(column_type, String):
        return str
    return Any
