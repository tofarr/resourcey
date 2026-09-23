"""``SqlService`` — the action layer of the ``v2`` SQL backend (issue #78).

The service holds the call-scoped ``ctx`` and the async session factory. On
enter it adopts storage already present in ``ctx`` (owned by whoever put it
there) or opens its own session and owns it; on exit it commits and closes only
what it opened — the storage-ownership rule from ``v2/core``.

``search`` implements keyset (seek) cursor pagination ordered by the identifier
field. The cursor is encrypted (tamper-proof) by an injected
:class:`~resourcey.v2.encryption.encryption_service.EncryptionService`; sorting
and filtering are deliberately out of scope for now.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.v2.core.dto import MISSING
from resourcey.v2.core.service import STORAGE_KEY, NotFoundError, Page, Service, ServiceError
from resourcey.v2.sql.cursor import decode_cursor, encode_cursor, keyset_predicate

if TYPE_CHECKING:
    from resourcey.v2.encryption.encryption_service import EncryptionService
    from resourcey.v2.sql.resource import SqlResource

T = TypeVar("T", bound=BaseModel)


class SqlService(Service[T]):
    """The standard actions over a SQL table, with keyset cursor pagination.

    Holds the call-scoped ``ctx`` and the session factory. On enter it adopts
    storage already present in ``ctx`` (owned by whoever put it there) or opens
    its own session and owns it; on exit it commits and closes only what it
    opened.
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
        insert_data = self._to_columns(data)
        result = await session.execute(table.insert().values(**insert_data))
        if id_field not in data:
            data[id_field] = result.inserted_primary_key[0]  # type: ignore[attr-defined]
        row = (
            (await session.execute(_by_id(self._resource.id_column, data[id_field])))
            .mappings()
            .first()
        )
        return self._to_dto(row)

    async def read(self, id: Any) -> T:  # noqa: A002
        """Fetch one DTO by id; raise :class:`NotFoundError` if absent."""
        session = self._active_session()
        found = (await session.execute(_by_id(self._resource.id_column, id))).mappings().first()
        if found is None:
            raise NotFoundError(id)
        return self._to_dto(found)

    async def update(self, id: Any, payload: T) -> T:  # noqa: A002
        """Apply the supplied (non-``MISSING``) fields of ``payload``; return the DTO."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        id_column = self._resource.id_column
        data = {k: v for k, v in _payload_values(payload).items() if k != id_field}
        existing = (await session.execute(_by_id(id_column, id))).mappings().first()
        if existing is None:
            raise NotFoundError(id)
        if data:
            columns = self._to_columns(data)
            await session.execute(update(table).where(id_column == id).values(**columns))
        return await self.read(id)

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete by id; raise :class:`NotFoundError` if absent."""
        session = self._active_session()
        table = self._resource.table
        id_column = self._resource.id_column
        existing = (await session.execute(_by_id(id_column, id))).mappings().first()
        if existing is None:
            raise NotFoundError(id)
        await session.execute(delete(table).where(id_column == id))

    async def search(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: Any = None,
    ) -> Page[T]:
        """Return up to ``limit`` rows by id, with keyset pagination via ``cursor``.

        Ordering is fixed to the identifier field; ``sort`` / ``desc`` /
        ``filters`` are accepted for interface compatibility but not yet
        implemented (a future PR adds them together).
        """
        if sort is not None or desc or filters is not None:
            raise NotImplementedError("v2 SqlService.search does not support sort/desc/filters yet")
        session = self._active_session()
        table = self._resource.table
        id_column = self._resource.id_column
        stmt = select(table).order_by(id_column.asc()).limit(limit + 1)
        if cursor is not None:
            stmt = stmt.where(keyset_predicate(id_column, self._decode(cursor)))
        rows = (await session.execute(stmt)).mappings().all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            self._next_cursor(page_rows[-1][id_column.name]) if has_more and page_rows else None
        )
        return Page(
            items=[self._to_dto(row) for row in page_rows], limit=limit, next_cursor=next_cursor
        )

    async def count(self, *, filters: Any = None) -> int:
        """Return the number of rows.

        ``filters`` is accepted for interface compatibility but not yet
        implemented, so — like :meth:`search` — passing one raises rather than
        silently returning the unfiltered total.
        """
        if filters is not None:
            raise NotImplementedError("v2 SqlService.count does not support filters yet")
        session = self._active_session()
        result = await session.execute(select(func.count()).select_from(self._resource.table))
        return int(result.scalar_one())

    async def batch_read(self, ids: list[Any]) -> list[T | None]:
        """Return DTOs positionally aligned with ``ids`` (``None`` for absent)."""
        session = self._active_session()
        table = self._resource.table
        id_column = self._resource.id_column
        rows = (await session.execute(select(table).where(id_column.in_(ids)))).mappings().all()
        by_id = {row[id_column.name]: self._to_dto(row) for row in rows}
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
    # Cursor helpers
    # ------------------------------------------------------------------

    def _encryption(self) -> EncryptionService:
        service = self._resource._encryption_service
        if service is None:
            raise ServiceError(
                "This SqlResource has no EncryptionService, so cursor pagination is "
                "unavailable; pass encryption_service= to the SqlResource."
            )
        return service

    def _decode(self, cursor: str) -> Any:
        return decode_cursor(self._encryption(), cursor)

    def _next_cursor(self, id_value: Any) -> str:
        return encode_cursor(self._encryption(), id_value)

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

    def _to_columns(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rename DTO field (attribute) keys to their table column names."""
        return {
            self._resource._column_for_attr.get(name, name): value for name, value in data.items()
        }

    def _to_dto(self, row: Any) -> T:
        """Build a DTO instance from a result row mapping (column names -> fields)."""
        values = {
            self._resource._attr_for_column.get(name, name): value for name, value in row.items()
        }
        return self._resource.get_dto_type().model_validate(values)


def _payload_values(payload: Any) -> dict[str, Any]:
    """The explicitly supplied (non-``MISSING``) fields of a DTO instance."""
    return {name: value for name, value in payload.__dict__.items() if value is not MISSING}


def _by_id(id_column: Any, value: Any) -> Any:
    """A ``SELECT`` for the row whose id column equals ``value``."""
    return select(id_column.table).where(id_column == value)
