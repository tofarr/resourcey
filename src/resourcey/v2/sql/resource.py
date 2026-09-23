"""The ``v2`` SQL backend: :class:`SqlResource` (issue #78).

``SqlResource`` is the SQL backend on top of
:class:`~resourcey.v2.core.resource.Resource`. It is handed an async session
maker (connection / config resolution is a separate concern, deferred) and
either:

* **adopts** the ORM model recorded by :func:`~resourcey.v2.sql.sqlalchemy_2_dto`
  in the DTO's metadata, or
* **generates** a model from the DTO declaration onto a declarative ``base``
  (default :data:`V2Base`) when no model is recorded.

Either way every resource materialises its model, so constructing the resources
of a manifest brings the whole current schema into the base's metadata — which
is what :func:`~resourcey.v2.sql.migration.generate_migration` enumerates.

The action layer lives in :mod:`resourcey.v2.sql.service`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Time,
    Uuid,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, registry

from resourcey.v2.core.dto import (
    DEFAULT_ID_FIELD_NAME,
    DTO,
    Missing,
    _is_union,
    _strip_annotated,
)
from resourcey.v2.core.resource import Resource, _camel_to_kebab, _pluralize
from resourcey.v2.core.service import Service, ServiceError
from resourcey.v2.sql.service import SqlService
from resourcey.v2.sql.sqlalchemy_2_dto import recorded_model

if TYPE_CHECKING:
    from sqlalchemy import Table

    from resourcey.v2.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)


class V2Base(DeclarativeBase):
    """The ``v2`` declarative base; generated models attach to it by default.

    A dedicated base keeps generated tables in a single registry owned by the
    framework while remaining compatible with async SQLAlchemy 2. Callers may
    bring their own base (pass ``base=``) or use this one directly.
    """

    registry = registry()


_SCALAR_COLUMNS: dict[Any, Any] = {
    str: String,
    int: Integer,
    bool: Boolean,
    float: Float,
    bytes: LargeBinary,
    Decimal: Numeric,
    datetime: DateTime(timezone=True),
    date: Date,
    time: Time,
    UUID: Uuid,
    dict: JSON,
    list: JSON,
}


class SqlResource(Resource[T]):
    """The SQL backend: an ORM model (adopted or generated) served by :class:`SqlService`.

    Args:
        dto: The DTO declaration this resource serves.
        session_factory: The async session maker the service opens sessions from.
        base: The declarative base a generated model attaches to (ignored when a
            model is already recorded in the DTO's metadata).
        path: An explicit REST path segment (defaults to the DTO name, pluralized).
        encryption_service: The service used to encrypt/decrypt pagination
            cursors; when omitted, cursors are unsupported.
    """

    def __init__(
        self,
        dto: type[DTO],
        *,
        session_factory: async_sessionmaker[AsyncSession],
        base: type[DeclarativeBase] = V2Base,
        path: str | None = None,
        encryption_service: EncryptionService | None = None,
    ) -> None:
        super().__init__(dto, path=path)
        self._session_factory = session_factory
        self._base = base
        self._encryption_service = encryption_service
        self._model = _model_for_dto(dto, base)
        self._column_for_attr = {
            prop.key: prop.columns[0].name for prop in self._model.__mapper__.column_attrs
        }
        self._attr_for_column = {col: attr for attr, col in self._column_for_attr.items()}

    # ------------------------------------------------------------------
    # Model / table surface
    # ------------------------------------------------------------------

    @property
    def model(self) -> type[Any]:
        """The ORM model: the adopted one, or the one generated from the DTO."""
        return self._model

    @property
    def table(self) -> Table:
        """The SQLAlchemy table the model maps to."""
        return cast("Table", self._model.__table__)

    @property
    def metadata(self) -> Any:
        """The metadata holding this resource's table.

        An adopted model keeps the base it was declared on (``base=`` is ignored
        in that case), so the metadata is read off the model's own table rather
        than ``self._base`` — otherwise ``resource.metadata.create_all`` would be
        a silent no-op for an adopted resource.
        """
        return self.table.metadata

    @property
    def id_column(self) -> Column[Any]:
        """The table column backing the identifier.

        The DTO field name is the mapper *attribute* name, which can differ from
        the *column* name for an adopted model, so the identifier must be
        resolved through the same attribute→column map the payload uses.
        """
        return cast("Column[Any]", self.table.c[self._column_for_attr[self.get_id_field()]])

    def build_service(self, ctx: MutableMapping[Any, Any]) -> Service[T]:
        """Build a :class:`SqlService` over ``ctx`` and the injected session factory."""
        return SqlService(self, ctx, self._session_factory)


# ---------------------------------------------------------------------------
# Model generation
# ---------------------------------------------------------------------------


def _model_for_dto(dto: type[DTO], base: type[DeclarativeBase]) -> type[Any]:
    """The ORM model for ``dto``: the recorded one, else generated onto ``base``."""
    adopted = recorded_model(dto)
    if adopted is not None:
        return adopted
    return _generate_model(dto, base)


def _generate_model(dto: type[DTO], base: type[DeclarativeBase]) -> type[Any]:
    """Generate an ORM model from a DTO declaration onto ``base``.

    A table of the derived name already present in the base's metadata is reused
    (so two resources for the same DTO — or a re-created resource in a test —
    share one table rather than colliding).
    """
    tablename = _table_name(dto)
    existing = base.metadata.tables.get(tablename)
    if existing is not None:
        return _model_for_existing_table(base, dto.__name__, existing)
    columns = [
        _column_for(name, annotation, dto.id_field_name)
        for name, (annotation, _cfg) in dto.__dto_fields__.items()
    ]
    return type(
        dto.__name__,
        (base,),
        {"__tablename__": tablename, **{column.name: column for column in columns}},
    )


def _model_for_existing_table(base: type[DeclarativeBase], name: str, table: Table) -> type[Any]:
    """A class for a table already in the base: reuse the mapped one, else map it."""
    for mapper in base.registry.mappers:
        if mapper.local_table is table:
            return mapper.class_
    return type(name, (base,), {"__table__": table})


def _table_name(dto: type[DTO]) -> str:
    """The table name derived from the DTO name (mirrors the REST path segment)."""
    return _pluralize(_camel_to_kebab(dto.__name__).lower().replace("-", "_"))


def _column_type(annotation: Any) -> Any:
    """Map a DTO field annotation to a SQLAlchemy column type."""
    concrete = _strip_annotated(annotation)
    for branch in _union_branches(concrete):
        if branch is type(None) or branch is Missing:
            continue
        if isinstance(branch, type) and issubclass(branch, Enum):
            return String
        column_type = _SCALAR_COLUMNS.get(branch)
        if column_type is not None:
            return column_type
    raise ServiceError(f"No SQL column type for DTO field annotation {annotation!r}")


def _union_branches(annotation: Any) -> tuple[Any, ...]:
    """The members of a union annotation, or a single-member tuple."""
    if _is_union(annotation):
        return tuple(annotation.__args__)
    return (annotation,)


def _column_for(field_name: str, annotation: Any, id_field_name: str) -> Column[Any]:
    """A column for one DTO field (the identifier is the primary key)."""
    column_type = _column_type(annotation)
    if field_name != id_field_name:
        return Column(field_name, column_type, nullable=_allows_none(annotation))
    # The conventional ``id`` is server-generated; an author-chosen identifier
    # is a natural key the caller supplies.
    if field_name == DEFAULT_ID_FIELD_NAME:
        if column_type is Integer:
            return Column(field_name, Integer, primary_key=True, autoincrement=True)
        # A non-integer id (e.g. the ``id: UUID`` convention) is client-supplied
        # and never in a create request, so generate it client-side rather than
        # inserting NULL.
        return Column(field_name, column_type, primary_key=True, default=uuid4)
    return Column(field_name, column_type, primary_key=True)


def _allows_none(annotation: Any) -> bool:
    """Whether an annotation admits ``None`` (i.e. the column is nullable)."""
    return any(branch is type(None) for branch in _union_branches(_strip_annotated(annotation)))
