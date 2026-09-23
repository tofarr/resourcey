"""Adopt an existing SQLAlchemy ORM model as a ``v2`` DTO (issue #78).

:func:`sqlalchemy_2_dto` converts an ORM model into a
:class:`~resourcey.v2.core.dto.DTO` declaration: each mapped column's
SQLAlchemy type maps back to the corresponding Python annotation, the primary
key becomes the DTO identifier (``id_field_name``), nullability becomes
``ann | None``, and server-generated defaults become ``in_create_request=False``
/ logical defaults.

The produced DTO carries the **original model** in its ``metadata`` under
:data:`MODEL_METADATA_KEY` — the free-form store on ``DTO`` that ``v2/core``
never reads. That is the handoff to the backend: a
:class:`~resourcey.v2.sql.resource.SqlResource` built from the DTO adopts the
recorded model; with no recorded model it generates one from the DTO.

**Scope: plain columns only.** Every column converts as an ordinary scalar
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

# The namespaced metadata key under which the source ORM model is recorded.
# A string keeps ``DTO.metadata``'s ``dict[str, Any]`` contract (a sentinel
# would not) and the ``v2.sql`` namespace rules out collision with user keys.
MODEL_METADATA_KEY = "v2.sql.model"


def sqlalchemy_2_dto(model: type[Any], *, name: str | None = None) -> type[DTO]:
    """Convert an ORM model into a DTO declaration that records the model.

    The DTO's fields mirror the model's mapped columns (attribute order), with
    the primary key as the DTO identifier. The original model is stored in
    ``DTO.metadata`` under :data:`MODEL_METADATA_KEY` so a backend can adopt it.

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
        metadata={MODEL_METADATA_KEY: model},
    )


def recorded_model(dto: type[DTO]) -> type[Any] | None:
    """The ORM model recorded in ``dto``'s metadata, or ``None``.

    ``DTO.metadata`` merges down the MRO, so a DTO subclass inherits its
    parent's recorded model — desirable for an adopted model.
    """
    recorded = dto.metadata.get(MODEL_METADATA_KEY)
    return recorded if isinstance(recorded, type) else None


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
    """The ``DtoField`` flags/defaults implied by a column's generation behaviour.

    A client-side default is re-expressed as the field's logical default and
    the field drops out of create requests; a server-side default or an
    auto-increment primary key does the same, minus a logical default (the
    database supplies the value).
    """
    default = column.default
    if default is not None:
        if getattr(default, "is_callable", False):
            # SQLAlchemy wraps a callable default in an ``(ctx)``-taking adapter;
            # unwrap to the author's callable so the DTO factory is arity-correct.
            factory = getattr(default.arg, "__wrapped__", default.arg)
            return DtoField(in_create_request=False, logical_default_value_factory=factory)
        return DtoField(in_create_request=False, logical_default_value=default.arg)
    if column.server_default is not None or _is_auto_increment(column, mapper):
        return DtoField(in_create_request=False)
    return DtoField()


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
