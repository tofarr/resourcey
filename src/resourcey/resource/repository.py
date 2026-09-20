"""Async SQLAlchemy data-access layer for a resource.

``ResourceRepository`` is bound to a ``BaseResource`` subclass and executes
against the resource's generated SQLAlchemy ORM model. It holds no
connection of its own: every method takes the caller's ``AsyncSession`` so
the caller controls the transaction boundary. Services contain logic;
repositories contain data access (``routers -> services -> repositories ->
models`` per ``AGENTS.md``).

Read-model hydration is a single helper (``_to_read_model``) so it is
individually testable. Secret-bearing fields are encrypted / decrypted via
the serialization ``context`` passed through from the service (see
:mod:`resourcey.util.secret_serialization`): create / update payloads are
dumped with the context (secrets -> ciphertext for storage) and rows are
validated into the read model with the same context (ciphertext ->
``SecretStr``).

Every method is a thin, single-purpose hook a subclass can replace (escape
hatch to raw SQLAlchemy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession

    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


class ResourceRepository:
    """Async data-access layer for a resource's generated ORM model.

    Constructed once from a ``BaseResource`` subclass; the ORM model, id
    field, and read model are resolved (and cached on the resource) at
    construction. Each async method takes an ``AsyncSession``.
    """

    def __init__(self, resource: type[BaseResource]) -> None:
        self.resource = resource
        self.model = resource.get_sql_alchemy_model()
        self.id_field = resource.get_id_field()
        self.read_model = resource.get_read_model()

    # ------------------------------------------------------------------
    # Read-model hydration
    # ------------------------------------------------------------------

    def _to_read_model(self, row: Any, context: dict[str, Any] | None) -> Any:
        """Project an ORM row into the resource's read-model instance.

        Reads each read-model field from the row attribute of the same name
        and validates into the read model with ``context`` so secret fields
        decrypt (ciphertext -> ``SecretStr``). A single helper so it is
        individually testable.
        """
        names = self.read_model.model_fields
        data = {name: getattr(row, name, None) for name in names}
        return self.read_model.model_validate(data, context=context)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def insert(
        self,
        session: AsyncSession,
        payload: BaseModel,
        *,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Persist a create-model instance; return the loaded read model.

        Optional fields whose value is ``MISSING`` (not supplied) are
        dropped. Non-creatable fields that declare a ``default_factory``
        (e.g. ``created_at``) are populated from it, since the create model
        excludes them but the row still needs a value. The id is generated
        by the DB (autoincrement for int ids).
        """
        data = self._dump_payload(payload, context, fill_defaults=True)
        row = self.model(**data)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise
        await session.refresh(row)
        return self._to_read_model(row, context)

    async def update_by_id(
        self,
        session: AsyncSession,
        id: Any,  # noqa: A002
        payload: BaseModel,
        *,
        context: dict[str, Any] | None = None,
    ) -> Any | None:
        """Apply a PATCH-style update model; return the read model or ``None``.

        Fields whose value is ``MISSING`` are skipped (PATCH semantics). A
        targeted row that does not exist returns ``None``.
        """
        data = self._dump_payload(payload, context)
        if not data:
            # Nothing to change; still return the row if it exists.
            return await self.get_by_id(session, id, context=context)
        stmt = select(self.model).where(self._id_column() == id)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        for name, value in data.items():
            setattr(row, name, value)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise
        await session.refresh(row)
        return self._to_read_model(row, context)

    async def delete_by_id(self, session: AsyncSession, id: Any) -> bool:  # noqa: A002
        """Delete the row; return ``True`` if a row was removed, ``False`` if absent."""
        stmt = delete(self.model).where(self._id_column() == id)
        result = await session.execute(stmt)
        await session.flush()
        return cast("int", result.rowcount) > 0  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_by_id(
        self,
        session: AsyncSession,
        id: Any,  # noqa: A002
        *,
        context: dict[str, Any] | None = None,
    ) -> Any | None:
        """Return the read-model instance or ``None``."""
        stmt = select(self.model).where(self._id_column() == id)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return self._to_read_model(row, context)

    async def get_many_by_ids(
        self,
        session: AsyncSession,
        ids: list[Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Return read-model instances for the given ids.

        Preserves input order and omits absent ids. Duplicate ids in the
        input are de-duplicated for the query then re-expanded to the input
        order so each requested id maps to at most one result.
        """
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        stmt = select(self.model).where(self._id_column().in_(unique_ids))
        rows = (await session.execute(stmt)).scalars().all()
        by_id = {getattr(row, self.id_field): row for row in rows}
        return [self._to_read_model(by_id[i], context) for i in unique_ids if i in by_id]

    async def search(
        self,
        session: AsyncSession,
        *,
        limit: int,
        offset: int,
        sort: tuple[str, bool] | None,
        filters: SearchFilter[Any] | None,
        context: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Build and execute a paged select; return read-model instances.

        ``filters`` (a :class:`~resourcey.util.search_filter.SearchFilter` or
        ``None``) contributes its SQL ``WHERE`` via ``filter_sql``.
        ``sort`` is a ``(field_name, ascending)`` tuple already validated by
        the service, or ``None`` for no ordering.
        """
        stmt = select(self.model)
        if filters is not None:
            stmt = filters.filter_sql(stmt)
        stmt = self._apply_sort(stmt, sort)
        stmt = stmt.limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()
        return [self._to_read_model(row, context) for row in rows]

    async def count(
        self,
        session: AsyncSession,
        *,
        filters: SearchFilter[Any] | None,
    ) -> int:
        """Total matching rows for pagination metadata (same ``filters``)."""
        stmt = select(func.count()).select_from(self.model)
        if filters is not None:
            stmt = filters.filter_sql(stmt)
        return (await session.execute(stmt)).scalar_one()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _id_column(self) -> Any:
        """The SQLAlchemy column for the resource's id field."""
        return getattr(self.model, self.id_field)

    def _dump_payload(
        self,
        payload: BaseModel,
        context: dict[str, Any] | None,
        *,
        fill_defaults: bool = False,
    ) -> dict[str, Any]:
        """Dump a create/update model to a column-value dict.

        ``exclude_unset=True`` drops fields the caller did not explicitly
        supply. For the update model that is PATCH semantics (omitted fields
        are left untouched). For the create model, ``fill_defaults=True``
        additionally populates defaults for non-supplied fields (both
        ``default_factory`` callables and plain ``default`` values, e.g.
        ``created_at`` / ``size``) so columns never receive NULL for a field
        that has a default.
        """
        from pydantic_core import PydanticUndefined

        data = payload.model_dump(context=context, exclude_unset=True)
        if not fill_defaults:
            return data
        for name, field in self.resource.model_fields.items():
            if name in data:
                continue
            if field.default_factory is not None:
                factory = cast("Callable[[], Any]", field.default_factory)
                data[name] = factory()
            elif field.default is not PydanticUndefined:
                data[name] = field.default
        return data

    def _apply_sort(
        self,
        stmt: Any,
        sort: tuple[str, bool] | None,
    ) -> Any:
        """Apply an ``order_by`` clause from a validated ``(field, ascending)`` tuple."""
        if sort is None:
            return stmt
        field_name, ascending = sort
        column = getattr(self.model, field_name)
        return stmt.order_by(column.asc() if ascending else column.desc())
