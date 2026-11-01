"""``SqlService`` — the action layer of the SQL backend (issue #78).

The service holds the call-scoped ``ctx`` and the async session factory. On
enter it adopts storage already present in ``ctx`` or opens its own session and
owns it; on exit it commits and closes only what it opened.

``search`` implements keyset cursor pagination ordered by the identifier by
default, or by a validated ``sort`` field (with the identifier as a stable
tie-breaker). The cursor encodes the sort it was built for, so a cursor reused
under a different sort is rejected rather than applied against the wrong column.

This module imports no code outside the framework.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.core.dto import apply_operation_defaults
from resourcey.core.errors import InvalidInputError, UnsupportedFilterError
from resourcey.core.service import (
    DEFAULT_LIMIT,
    Action,
    Create,
    Delete,
    NotFoundError,
    Page,
    Service,
    ServiceError,
    Update,
    storage_key,
)
from resourcey.sql.cursor import decode_cursor, encode_cursor, keyset_predicate
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.encryption.encryption_service import EncryptionService
    from resourcey.sql.sql_resource import SqlResource

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


def sql_storage_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """What a SQL service's storage key names: the bound engine, else the maker.

    Two resources over one engine share a session (and so a transaction) even
    when each was handed its own maker; two over different engines never do.
    An unbound maker can only be identified by itself.
    """
    bind = getattr(session_factory, "kw", {}).get("bind")
    return bind if bind is not None else session_factory


class SqlService(Service[T, K]):
    """The standard actions over a SQL table, with keyset cursor pagination."""

    def __init__(
        self,
        resource: SqlResource[T, K],
        ctx: MutableMapping[Any, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._resource = resource
        self._ctx = ctx
        self._session_factory = session_factory
        self._storage_key = storage_key("sql", sql_storage_identity(session_factory))
        self._session: AsyncSession | None = None
        self._owns_storage = False

    # ------------------------------------------------------------------
    # Lifecycle + storage ownership
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SqlService[T, K]:
        await super().__aenter__()
        session = self._ctx.get(self._storage_key)
        if session is None:
            session = self._session_factory()
            self._ctx[self._storage_key] = session
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
            self._ctx.pop(self._storage_key, None)
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
        """Insert a DTO, filling omitted fields from create defaults; return the DTO."""
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        data = self._with_defaults(_payload_values(payload), "create")
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

    async def read(self, id: K) -> T:  # noqa: A002
        """Fetch one DTO by id; raise :class:`NotFoundError` if absent."""
        session = self._active_session()
        found = (await session.execute(_by_id(self._resource.id_column, id))).mappings().first()
        if found is None:
            raise NotFoundError(id)
        return self._to_dto(found)

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        """Apply an update DTO (whose ``id`` field names the row); return the DTO or ``None``.

        Supplied fields are written; an omitted field with no update default is
        left unchanged (PATCH semantics). The id is never written.

        ``None`` means no row was written — an absent id or a failed
        ``condition`` — so the two are indistinguishable.
        """
        session = self._active_session()
        table = self._resource.table
        id_field = self._resource.get_id_field()
        id_column = self._resource.id_column
        values = _payload_values(payload)
        id_value = values.get(id_field)
        if id_value is MISSING or id_value is None:
            raise ServiceError("update requires the identifier on the payload")
        data = {k: v for k, v in values.items() if k != id_field}
        data = self._with_defaults(data, "update")
        if data:
            stmt = update(table).where(id_column == id_value).values(**self._to_columns(data))
            if condition is not None:
                stmt = await self._apply_condition(stmt, session, condition)
            result = await session.execute(stmt)
            if _rowcount(result) == 0:
                return None
            return await self.read(id_value)
        # No columns to write (an empty PATCH): the row exists iff it satisfies
        # the condition, so evaluate that with a SELECT rather than an UPDATE.
        found_stmt = _by_id(id_column, id_value)
        if condition is not None:
            found_stmt = await self._apply_condition(found_stmt, session, condition)
        found = (await session.execute(found_stmt)).mappings().first()
        return None if found is None else self._to_dto(found)

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        """Delete by id; return whether a row was deleted.

        ``False`` means no row was deleted — an absent id or a failed
        ``condition`` — so the two are indistinguishable.
        """
        session = self._active_session()
        table = self._resource.table
        id_column = self._resource.id_column
        stmt = delete(table).where(id_column == id)
        if condition is not None:
            stmt = await self._apply_condition(stmt, session, condition)
        result = await session.execute(stmt)
        return _rowcount(result) > 0

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        """Return up to ``limit`` rows, with keyset pagination via ``cursor``.

        The filter is pushed into the ``WHERE`` clause before the page is taken,
        and the next cursor is derived from the *same* ``sort_order`` the page
        was ordered and sought by.
        """
        session = self._active_session()
        table = self._resource.table
        stmt = select(table).limit(limit + 1)
        stmt = self._apply_sort(stmt, sort_order)
        stmt = await self._apply_filters(stmt, session, search_filter)
        if cursor is not None:
            stmt = stmt.where(self._cursor_predicate(cursor, sort_order))
        rows = (await session.execute(stmt)).mappings().all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            self._next_cursor(page_rows[-1], sort_order) if has_more and page_rows else None
        )
        return Page(
            items=[self._to_dto(row) for row in page_rows],
            limit=limit,
            next_cursor=next_cursor,
        )

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        """Return the number of rows matching ``search_filter`` (all rows when ``None``)."""
        session = self._active_session()
        stmt = select(func.count()).select_from(self._resource.table)
        stmt = await self._apply_filters(stmt, session, search_filter)
        result = await session.execute(stmt)
        return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Filter pushdown
    # ------------------------------------------------------------------

    async def _apply_filters(
        self, stmt: Any, session: AsyncSession, search_filter: SearchFilter[Any] | None
    ) -> Any:
        """Push ``search_filter`` into ``stmt``'s WHERE clause (all-or-nothing).

        An unconvertible node raises :class:`UnsupportedFilterError` unless the
        resource opted into the in-memory iteration fallback, because silently
        skipping a filter could leak every row of a permission scope.
        """
        if search_filter is None:
            return stmt
        standard = search_filter.create_standard_filter()
        converter = self._resource.build_filter_converter(session)
        try:
            await converter.resolve(standard)
            return converter.apply(stmt, standard)
        except UnsupportedFilterError:
            if not self._resource.allow_filter_iteration:
                raise
            return await self._apply_filters_by_iteration(stmt, standard)

    async def _apply_filters_by_iteration(self, stmt: Any, standard: Any) -> Any:
        """The opt-in fallback: materialise matching ids and constrain to them.

        Only used when a resource explicitly sets
        ``allow_filter_iteration = True`` — an unbounded scan behind a public GET
        is otherwise a DoS.
        """
        session = self._active_session()
        rows = (await session.execute(select(self._resource.table))).mappings().all()
        dto_type = self._resource.get_dto_type()
        surviving = [
            row[self._resource.id_column.name]
            for row in rows
            if standard.matches(dto_type.model_validate(_row_values(self._resource, row)))
        ]
        return stmt.where(self._resource.id_column.in_(surviving))

    async def _apply_condition(
        self, stmt: Any, session: AsyncSession, condition: SearchFilter[Any]
    ) -> Any:
        """Push a write's ``condition`` into ``stmt``'s WHERE clause.

        Unlike :meth:`_apply_filters`, the in-memory fallback is **refused** even
        when the resource opted into it: materialising ids and then writing would
        open a TOCTOU window. An unconvertible condition raises, so the caller
        fails closed.
        """
        standard = condition.create_standard_filter()
        converter = self._resource.build_filter_converter(session)
        await converter.resolve(standard)
        return converter.apply(stmt, standard)

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        """Return DTOs positionally aligned with ``ids`` (``None`` for absent)."""
        session = self._active_session()
        table = self._resource.table
        id_column = self._resource.id_column
        rows = (await session.execute(select(table).where(id_column.in_(ids)))).mappings().all()
        by_id = {row[id_column.name]: self._to_dto(row) for row in rows}
        return [by_id.get(i) for i in ids]

    async def apply_edits(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Apply each :class:`Edit`; results align positionally with ``edits``.

        A create / update yields the resulting DTO; a delete or a miss yields
        ``None``. A node is refused unless the resource *declares* the matching
        action, so a batch can never reach an action the resource does not
        expose.
        """
        supported = self._resource.get_supported_actions()
        results: list[T | None] = []
        for edit in edits:
            if isinstance(edit, Create):
                if Action.CREATE not in supported:
                    raise InvalidInputError("a batch cannot create: create is not supported")
                results.append(await self.create(edit.item))
            elif isinstance(edit, Update):
                if Action.UPDATE not in supported:
                    raise InvalidInputError("a batch cannot update: update is not supported")
                results.append(await self.update(edit.item, condition=edit.condition))
            else:
                if Action.DELETE not in supported:
                    raise InvalidInputError("a batch cannot delete: delete is not supported")
                await self.delete(edit.id, condition=edit.condition)
                results.append(None)
        return results

    # ------------------------------------------------------------------
    # Cursor + sort helpers
    # ------------------------------------------------------------------

    def _encryption(self) -> EncryptionService:
        return self._resource._encryption_service

    def _apply_sort(self, stmt: Any, sort_order: SortOrder[Any] | None) -> Any:
        """Order ``stmt`` by ``sort_order`` (default: the identifier ascending)."""
        if sort_order is None:
            return stmt.order_by(self._resource.id_column.asc())
        return self._resource.build_sort_converter().apply(stmt, sort_order)

    def _cursor_predicate(self, cursor: str, sort_order: SortOrder[Any] | None) -> Any:
        """The keyset ``WHERE`` clause for ``cursor``, bound to the request's sort.

        A cursor built for a different ``(sort field, direction)`` is rejected
        rather than applied against the wrong column.
        """
        cursor_field, cursor_ascending, sort_key, id_value = self._decode(cursor)
        attribute = getattr(sort_order, "attribute", None)
        descending = bool(getattr(sort_order, "descending", False))
        expected_ascending = not descending
        if cursor_field != attribute or cursor_ascending != expected_ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; "
                "start a new search without a cursor when changing sort."
            )
        id_column = self._resource.id_column
        if sort_order is None:
            return keyset_predicate(
                sort_column=id_column,
                id_column=id_column,
                cursor_key=sort_key,
                cursor_id=id_value,
                ascending=True,
            )
        return keyset_predicate(
            sort_column=self._resource.build_sort_context().column_for(cast("str", attribute)),
            id_column=id_column,
            cursor_key=sort_key,
            cursor_id=id_value,
            ascending=cursor_ascending,
        )

    def _decode(self, cursor: str) -> tuple[str | None, bool, Any, Any]:
        """Decrypt and validate a cursor, mapping malformed input to a 400."""
        try:
            return decode_cursor(self._encryption(), cursor)
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc

    def _next_cursor(self, row: Any, sort_order: SortOrder[Any] | None) -> str:
        """Encode the cursor pointing past ``row`` under the resolved ``sort_order``.

        The sort state is read from ``sort_order``, not the raw request, so the
        emitted cursor always matches the ordering the page was built with.
        """
        id_column = self._resource.id_column
        id_value = row[id_column.name]
        attribute = getattr(sort_order, "attribute", None)
        sort_field = attribute if attribute is not None else None
        ascending = not bool(getattr(sort_order, "descending", False))
        if attribute is None:
            sort_key = id_value
        else:
            # The row is keyed by *column* names; the sort field is a DTO
            # attribute, which can differ from the column name.
            column_name = self._resource.get_column_name(attribute)
            sort_key = row[column_name]
        return encode_cursor(
            self._encryption(),
            sort_field=sort_field,
            ascending=ascending,
            sort_key=sort_key,
            id_value=id_value,
        )

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    def _with_defaults(self, data: dict[str, Any], operation: str) -> dict[str, Any]:
        """Fill omitted fields from the DTO's defaults for ``operation``."""
        return apply_operation_defaults(self._resource._dto, data, operation)

    def _to_columns(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rename DTO field (attribute) keys to their table column names.

        A ``SecretStr`` value is unwrapped at this storage boundary: the DBAPI
        cannot bind one.
        """
        return {
            self._resource._column_for_attr.get(name, name): _unbind_secret(value)
            for name, value in data.items()
        }

    def _to_dto(self, row: Any) -> T:
        """Build a DTO instance from a result row mapping (column names -> fields)."""
        return self._resource.get_dto_type().model_validate(_row_values(self._resource, row))


def _row_values(resource: Any, row: Any) -> dict[str, Any]:
    """Map a result row's column names back to DTO attribute names."""
    return {resource._attr_for_column.get(name, name): value for name, value in row.items()}


def _unbind_secret(value: Any) -> Any:
    """Unwrap a :class:`~pydantic.SecretStr` to its plaintext for DBAPI binding."""
    from pydantic import SecretStr

    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _payload_values(payload: Any) -> dict[str, Any]:
    """The explicitly supplied (non-``MISSING``) fields of a DTO instance."""
    return {name: value for name, value in payload.__dict__.items() if value is not MISSING}


def _by_id(id_column: Any, value: Any) -> Any:
    """A ``SELECT`` for the row whose id column equals ``value``."""
    return select(id_column.table).where(id_column == value)


def _rowcount(result: Any) -> int:
    """The affected-row count of an ``UPDATE`` / ``DELETE``.

    SQLAlchemy's generic ``Result`` does not declare ``rowcount``; only the
    DML-returning ``CursorResult`` does.
    """
    return int(cast("CursorResult[Any]", result).rowcount)
