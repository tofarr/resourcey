"""``Resource`` — the declaration derived from a DTO — and the SQL backend.

A :class:`Resource` is *derived from* a :class:`~resourcey.v2.core.dto.DTO`: the
DTO is the field-level declaration, the resource serves it through a service.
The backend is chosen explicitly at construction, never inferred from app
config::

    manifest = Manifest(resources=[SqlResource(Thread, session_factory=factory)])

The DTO is a prerequisite for a service, so a resource is constructed from the
DTO class plus whatever its backend needs (for SQL, an async session factory).

``ctx`` and storage ownership
-----------------------------
:meth:`Resource.get_service` is **sync** and takes an optional call-scoped
``MutableMapping``; the returned :class:`~resourcey.v2.core.service.Service` is
the async context manager that owns the storage lifetime. This keeps two
storage strategies expressible and privileges neither:

* **session-per-service** — the dependency caches a session in the call
  context, shares it across services, and commits/rolls back at the end;
* **session-per-operation** — the service holds a session factory and opens a
  session per action.

The rule that lets both coexist:

    Whoever opens the storage owns its commit and close. A resource that finds
    storage already in ``ctx`` reuses it and neither commits nor closes it.

``ctx`` is a plain ``MutableMapping`` keyed by module-level sentinels
(:data:`~resourcey.v2.core.service.STORAGE_KEY`), so an escape-hatch caller can
pre-seed storage and every resource in the call adopts it. ``AppContext``
(app-scoped: config, factories, clients) is a separate concept — ``ctx`` must
not absorb it.

``get_supported_actions()`` is the single action declaration (there is no
``actions`` property); exposure composes on top of it, and the exposed
resource's declaration wins outright.

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, MutableMapping
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Generic, TypeVar, cast
from uuid import UUID

from fastapi import Request
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
    MetaData,
    String,
    Table,
    Time,
    Uuid,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.v2.core.dto import (
    DEFAULT_ID_FIELD_NAME,
    DTO,
    MISSING,
    DtoField,
    Missing,
    RestModels,
    _is_union,
    _strip_annotated,
)
from resourcey.v2.core.service import (
    STORAGE_KEY,
    Action,
    CacheStrategy,
    NotFoundError,
    Page,
    Service,
    ServiceError,
)

T = TypeVar("T", bound=BaseModel)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z]{2})(?=[A-Z][a-z])")


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


class Resource(Generic[T]):
    """A resource derived from a DTO declaration, served through a service.

    The base is storage-agnostic: it knows the DTO, the derived REST models,
    and the action surface, but not where the data lives. A backend subclasses
    it and implements :meth:`build_service` (see :class:`SqlResource`).
    """

    def __init__(self, dto: type[DTO], *, path: str | None = None) -> None:
        self._dto = dto
        self._path = path
        self._entered = False

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The generated DTO model — the single internal type to work with."""
        return cast(type[T], self._dto.get_dto_type())

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the DTO's ``in_*`` flags."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name, from the DTO declaration (``id`` by default)."""
        return self._dto.id_field_name

    def get_search_filter_type(self) -> type | None:
        """The declared search-filter class, or ``None`` for no filtering."""
        return None

    def get_cache_strategy(self) -> CacheStrategy | None:
        """The cache strategy for this resource, or ``None`` (no caching) by default."""
        return None

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the DTO name, pluralized."""
        if self._path is not None:
            return self._path.lstrip("/")
        return _pluralize(_camel_to_kebab(self._dto.__name__).lower())

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        """The actions this resource serves — the single action declaration.

        Defaults to every :class:`Action`. A subclass narrows by overriding; it
        must never widen beyond ``frozenset(Action)``. The startup assertion in
        :class:`~resourcey.v2.core.manifest.Manifest` rejects non-``Action``
        members so a typo fails loudly instead of silently dropping a route.
        """
        return frozenset(Action)

    def get_exposed_resource(self) -> Resource[T] | None:
        """The resource the outside world sees (default: ``self``).

        ``None`` means internal-only. The exposed resource's
        :meth:`get_supported_actions` wins outright — no union with an inner
        resource.
        """
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T]:
        """Build the service for this resource over the call-scoped ``ctx``.

        Sync: the returned :class:`~resourcey.v2.core.service.Service` is the
        async context manager that opens (and, when it opened them, commits and
        closes) the storage. Pass ``ctx`` to share storage across services.
        """
        return self.build_service(ctx if ctx is not None else {})

    def build_service(self, ctx: MutableMapping[Any, Any]) -> Service[T]:
        """Build the service bound to ``ctx`` (backend seam; the base raises)."""
        raise ServiceError(
            f"{type(self).__name__} has no storage backend; subclass it (e.g. SqlResource) "
            "and implement build_service()."
        )

    async def get_service_dependency(self, request: Request) -> AsyncIterator[Service[T]]:
        """Yield the entered per-request service (usable directly as a FastAPI dependency).

        The per-request seam a route builder consumes. It resolves the
        request-scoped ``ctx`` (so every resource in one request shares
        storage), builds the service, and enters it for the caller.
        """
        service = self.get_service(_request_ctx(request))
        async with service:
            yield service

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Resource[T]:
        """Enter the resource's runtime lifecycle (guards against double entry)."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the resource's runtime lifecycle."""
        self._entered = False


# ---------------------------------------------------------------------------
# SQL backend
# ---------------------------------------------------------------------------


class SqlResource(Resource[T]):
    """The SQL backend: a DTO-derived table served by :class:`SqlService`.

    The table is generated from the DTO annotation (one column per field) and
    the storage is an injected async session factory, so the backend is chosen
    explicitly at construction and no app config is read here::

        SqlResource(Thread, session_factory=async_sessionmaker(engine))
    """

    def __init__(
        self,
        dto: type[DTO],
        *,
        session_factory: async_sessionmaker[AsyncSession],
        path: str | None = None,
    ) -> None:
        super().__init__(dto, path=path)
        self._session_factory = session_factory
        self._metadata = MetaData()
        self._table = _build_table(dto, self._metadata)

    @property
    def table(self) -> Table:
        """The generated SQLAlchemy table (create it in a test / migration)."""
        return self._table

    @property
    def metadata(self) -> MetaData:
        """The metadata holding this resource's table."""
        return self._metadata

    def build_service(self, ctx: MutableMapping[Any, Any]) -> Service[T]:
        """Build a :class:`SqlService` over ``ctx`` and the injected session factory."""
        return SqlService(self, ctx, self._session_factory)


class SqlService(Service[T]):
    """The SQL service: the standard actions over a DTO-derived table.

    Holds the call-scoped ``ctx`` and the session factory. On enter it *adopts*
    storage already present in ``ctx`` (owned by whoever put it there) or opens
    its own session and owns it; on exit it commits and closes only what it
    opened. Actions translate between DTO instances and table rows.
    """

    def __init__(
        self,
        resource: SqlResource[T],
        ctx: MutableMapping[Any, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._resource = resource
        self._ctx = ctx
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._owns_storage = False

    # ------------------------------------------------------------------
    # Lifecycle + storage ownership
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SqlService[T]:
        await super().__aenter__()
        session = self._ctx.get(STORAGE_KEY)
        if session is None:
            session = self._session_factory()
            self._ctx[STORAGE_KEY] = session
            self._owns_storage = True
        self._session = cast(AsyncSession, session)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_storage and self._session is not None:
            if exc[0] is not None:
                await self._session.rollback()
            else:
                await self._session.commit()
            await self._session.close()
            self._ctx.pop(STORAGE_KEY, None)
        await super().__aexit__(*exc)

    def _active_session(self) -> AsyncSession:
        self._require_entered()
        if self._session is None:  # pragma: no cover - guarded by _require_entered
            raise ServiceError(f"{type(self).__name__} has no session")
        return self._session

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        """Insert a DTO, filling omitted fields from logical defaults; return the DTO."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        data = self._with_logical_defaults(_payload_values(payload))
        result = await session.execute(table.insert().values(**data))
        if id_field not in data:
            data[id_field] = result.inserted_primary_key[0]  # type: ignore[attr-defined]
        row = (await session.execute(_by_id(table, id_field, data[id_field]))).mappings().first()
        return self._to_dto(row)

    async def read(self, id: Any) -> T:  # noqa: A002
        """Fetch one DTO by id; raise :class:`NotFoundError` if absent."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        found = (await session.execute(_by_id(table, id_field, id))).mappings().first()
        if found is None:
            raise NotFoundError(id)
        return self._to_dto(found)

    async def update(self, id: Any, payload: T) -> T:  # noqa: A002
        """Apply the supplied (non-``MISSING``) fields of ``payload``; return the DTO."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        data = {k: v for k, v in _payload_values(payload).items() if k != id_field}
        existing = (await session.execute(_by_id(table, id_field, id))).mappings().first()
        if existing is None:
            raise NotFoundError(id)
        if data:
            await session.execute(update(table).where(table.c[id_field] == id).values(**data))
        return await self.read(id)

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete by id; raise :class:`NotFoundError` if absent."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        existing = (await session.execute(_by_id(table, id_field, id))).mappings().first()
        if existing is None:
            raise NotFoundError(id)
        await session.execute(delete(table).where(table.c[id_field] == id))

    async def search(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: Any = None,
    ) -> Page[T]:
        """Return up to ``limit`` rows, optionally ordered by ``sort``."""
        session = self._active_session()
        table = self._resource.table
        sort_field = sort or self._resource.get_id_field()
        if sort_field not in table.c:
            raise NotFoundError(sort_field)
        column = table.c[sort_field]
        stmt = select(table).order_by(column.desc() if desc else column.asc()).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
        return Page(items=[self._to_dto(row) for row in rows], limit=limit, next_cursor=None)

    async def count(self, *, filters: Any = None) -> int:
        """Return the number of rows."""
        session = self._active_session()
        result = await session.execute(select(func.count()).select_from(self._resource.table))
        return int(result.scalar_one())

    async def batch_read(self, ids: list[Any]) -> list[T | None]:
        """Return DTOs positionally aligned with ``ids`` (``None`` for absent)."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        rows = (
            (await session.execute(select(table).where(table.c[id_field].in_(ids))))
            .mappings()
            .all()
        )
        by_id = {row[id_field]: self._to_dto(row) for row in rows}
        return [by_id.get(i) for i in ids]

    async def batch_edit(self, edits: list[tuple[Any, T]]) -> list[T | None]:
        """Apply each ``(id, payload)`` edit; results align positionally with ``edits``."""
        results: list[T | None] = []
        for edit_id, payload in edits:
            try:
                results.append(await self.update(edit_id, payload))
            except NotFoundError:
                results.append(None)
        return results

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    def _with_logical_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fill omitted non-id fields from the DTO's logical defaults."""
        id_field = self._resource.get_id_field()
        for name, (_ann, config) in self._resource._dto.__dto_fields__.items():
            if name in data or name == id_field:
                continue
            default = config.resolve_default()
            if default is not MISSING:
                data[name] = default
        return data

    def _to_dto(self, row: Any) -> T:
        """Build a DTO instance from a result row mapping."""
        return self._resource.get_dto_type().model_validate(dict(row))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_ctx(request: Request) -> MutableMapping[Any, Any]:
    """The call-scoped context for ``request`` (created on first use)."""
    ctx = getattr(request.state, "_v2_ctx", None)
    if ctx is None:
        ctx = {}
        request.state._v2_ctx = ctx
    return cast(MutableMapping[Any, Any], ctx)


def _payload_values(payload: BaseModel) -> dict[str, Any]:
    """The explicitly supplied (non-``MISSING``) fields of a DTO instance."""
    return {name: value for name, value in payload.__dict__.items() if value is not MISSING}


def _by_id(table: Table, id_field: str, value: Any) -> Any:
    """A ``SELECT`` for the row whose id column equals ``value``."""
    return select(table).where(table.c[id_field] == value)


def _camel_to_kebab(name: str) -> str:
    """Insert ``-`` boundaries into a CamelCase identifier (kept local to core)."""
    return _CAMEL_BOUNDARY.sub("-", name)


def _pluralize(name: str) -> str:
    """Append a simple English plural suffix (kept local to core)."""
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


_SCALAR_COLUMNS: dict[Any, Any] = {
    str: String,
    int: Integer,
    bool: Boolean,
    float: Float,
    bytes: LargeBinary,
    datetime: DateTime(timezone=True),
    date: Date,
    time: Time,
    UUID: Uuid,
    dict: JSON,
    list: JSON,
}


def _union_branches(annotation: Any) -> tuple[Any, ...]:
    """The members of a union annotation, or a single-member tuple."""
    if _is_union(annotation):
        return tuple(annotation.__args__)
    return (annotation,)


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


def _column_for(field_name: str, annotation: Any, id_field_name: str) -> Column[Any]:
    """A column for one DTO field (the identifier is the primary key)."""
    column_type = _column_type(annotation)
    if field_name != id_field_name:
        return Column(field_name, column_type, nullable=True)
    # Only the conventional ``id`` is server-generated; an author-chosen
    # identifier is a natural key the caller supplies.
    if field_name == DEFAULT_ID_FIELD_NAME and column_type is Integer:
        return Column(field_name, Integer, primary_key=True, autoincrement=True)
    return Column(field_name, column_type, primary_key=True)


def _dto_fields(dto: type[DTO]) -> list[tuple[str, Any, DtoField]]:
    """The DTO declaration's ``(name, annotation, DtoField)`` triples in order."""
    return [(name, ann, cfg) for name, (ann, cfg) in dto.__dto_fields__.items()]


def _build_table(dto: type[DTO], metadata: MetaData) -> Table:
    """Generate a SQLAlchemy table from a DTO declaration (one column per field)."""
    name = _pluralize(_camel_to_kebab(dto.__name__).lower().replace("-", "_"))
    columns = [
        _column_for(field_name, annotation, dto.id_field_name)
        for field_name, annotation, _cfg in _dto_fields(dto)
    ]
    return Table(name, metadata, *columns)
