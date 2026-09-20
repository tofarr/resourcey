"""``SqlResource`` - the SQL-backed resource declaration layer.

``BaseResource`` (see :mod:`resourcey.resource.base`) is storage-agnostic: it
handles field collection and the generated Pydantic create / read / update
models. ``SqlResource`` extends it with the SQLAlchemy concerns: the
resourcey async declarative base (:class:`ResourceyBase`), table-name
derivation, per-field ``Column`` generation, and the generated ORM model.

A resourcey app's resources subclass ``SqlResource`` (directly or via a
further specialised base) and call :meth:`SqlResource.get_sql_alchemy_model`
to materialise the ORM model whose table lands in
:data:`ResourceyBase.metadata` for migrations / table creation.
"""

from __future__ import annotations

import enum
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, SecretStr
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

from resourcey.resource.base import BaseResource, _resolve_scalar_type
from resourcey.resource.errors import ResourceyConfigError
from resourcey.util.naming import camel_to_snake, pluralize


class ResourceyBase(DeclarativeBase):
    """resourcey-specific async SQLAlchemy declarative base.

    Generated ORM models extend this. Using a dedicated base (rather than a
    caller's own ``DeclarativeBase``) keeps generated tables in a single
    registry owned by the framework while remaining fully compatible with the
    async SQLAlchemy 2 ORM. Callers can still bring their own declarative base
    - this one is only used for generated models.
    """

    registry = registry()


# Default scalar type -> SQLAlchemy column type mapping used by
# ``get_column_for_field``. Any type not present here raises
# ``ResourceyConfigError`` so the developer supplies an explicit
# ``ResourceyField.column``.
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


class SqlResource(BaseResource):
    """A resource declaration backed by a generated SQLAlchemy ORM model.

    Adds the SQL concerns on top of :class:`BaseResource`: the table name,
    per-field column generation, the cached ORM model whose table is
    registered in :data:`ResourceyBase.metadata`, and the per-request
    :meth:`open_service` that yields a
    :class:`~resourcey.resource.service.SqlService` bound to a session.
    """

    _sqlalchemy_model: Any
    # Session factory used by ``open_service`` to open a per-request session.
    # Set via :meth:`configure` (typically by ``create_app``) before the app
    # serves requests. ``None`` means unconfigured. ``ClassVar`` keeps it out
    # of the Pydantic model_fields collection (it is config, not a column).
    _session_factory: ClassVar[Any] = None

    # ------------------------------------------------------------------
    # Registration hook
    # ------------------------------------------------------------------

    @classmethod
    def _on_register(cls) -> None:
        """Eagerly build the ORM model so its table lands in metadata.

        Called by :func:`~resourcey.resource.registry.register_resource` when
        a resource is registered. Building the model here (rather than lazily)
        ensures the derived table is in ``ResourceyBase.metadata`` before
        migrations or table creation run.
        """
        cls.get_sql_alchemy_model()

    # ------------------------------------------------------------------
    # Service + session configuration
    # ------------------------------------------------------------------

    @classmethod
    def get_service_cls(cls) -> type[Any]:
        """The service class this resource yields: :class:`SqlService`."""
        from resourcey.resource.service import SqlService

        return SqlService

    @classmethod
    def get_session_factory(cls) -> Any:
        """The configured ``async_sessionmaker`` used by :meth:`open_service`.

        Set via :meth:`configure` (typically by ``create_app``). Raises if it
        has not been configured - a SQL resource cannot open a session without
        one.
        """
        factory = cls.__dict__.get("_session_factory")
        if factory is None:
            raise ResourceyConfigError(
                f"{cls.__name__} has no session factory configured; call "
                f"{cls.__name__}.configure(session_factory=...) (create_app does this)."
            )
        return factory

    @classmethod
    def configure(cls, *, session_factory: Any) -> None:
        """Bind the ``async_sessionmaker`` used by :meth:`open_service`.

        Typically called once by :func:`resourcey.app.create_app` for each
        registered resource so all resources share one session factory (and
        thus one transaction per request via ``request.state``).
        """
        cls._session_factory = session_factory

    @classmethod
    def open_service(cls, request: Any) -> Any:
        """Async context manager yielding a :class:`SqlService` for ``request``.

        Opens a session (or reuses one already on ``request.state.session`` so
        multiple resources in one request share a single transaction), yields
        a :class:`~resourcey.resource.service.SqlService` bound to it, and
        commits / closes on exit. Suitable for use as an injected FastAPI
        dependency.
        """
        return _open_sql_service(cls, request)

    # ------------------------------------------------------------------
    # SQLAlchemy model generation
    # ------------------------------------------------------------------

    @classmethod
    def get_table_name(cls) -> str:
        """Derive the SQL table name from the class name.

        Snake-case the class name (``UserRole`` -> ``user_role``), then
        pluralize - appending ``"s"`` or ``"es"`` per the common endings
        (``s`` / ``x`` / ``z`` / ``ch`` / ``sh``) - and lowercase. Irregular
        plurals are left to an override. Overridable.
        """
        return pluralize(camel_to_snake(cls.__name__)).lower()

    @classmethod
    def get_column_for_field(cls, field_name: str, field: FieldInfo) -> Column[Any]:
        """Generate a SQLAlchemy ``Column`` for a field.

        Honours an explicit ``ResourceyField.column`` when provided. Otherwise
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
                "ResourceyField(column=Column(...)) for this field."
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
            "Supply an explicit ResourceyField(column=Column(...)) for this field."
        )
    return col_type


@asynccontextmanager
async def _open_sql_service(cls: type[SqlResource], request: Any) -> Any:
    """Open (or reuse) a session and yield a :class:`SqlService` for ``request``.

    If ``request.state.session`` already holds a session (opened by another
    resource in the same request), it is reused so all resources share one
    transaction; the caller that opened it owns the commit/close. Otherwise a
    new session is opened from the resource's session factory, stored on
    ``request.state.session``, committed on success, and closed on exit.
    """
    from resourcey.resource.service import SqlService

    session = getattr(request.state, "session", None)
    if session is not None:
        yield SqlService(cls, session=session)
        return
    factory = cls.get_session_factory()
    async with factory() as session:
        request.state.session = session
        try:
            yield SqlService(cls, session=session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
