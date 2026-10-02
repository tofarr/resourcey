"""``SqlFileStore`` -- file bytes in an internal blob table (issue #117, #158).

For deployments that already run Postgres and want no second system: the bytes
live in a dedicated ``file_blobs`` table, accessed through SQLAlchemy directly
(the documented escape hatch), and the framework signs the capability URLs
(:mod:`resourcey.filestore.signed_url`). It suits small files; large files
belong on S3.

Unlike #117's original design, the blob table carries ``name`` / ``checksum``
alongside the bytes -- **it is the single source of truth** for an object's
existence and metadata, exactly as the local directory and S3 bucket are for
their media. It is exposed as a real, registered, **read-only** resource
(:func:`sql_file_blob_view`: a :class:`~resourcey.sql.sql_resource.SqlResource`
wrapped in a :class:`~resourcey.view.ResourceView` that projects ``data`` out of
every response shape) for debuggability -- mirroring the OAuth token table's
"exposed read-only with every secret field hidden" pattern -- but the actual
``files`` surface (create / read / delete / search / count / batch) is served
by :mod:`resourcey.filestore.file_resource` through this store's ``put`` /
``head`` / ``list_objects`` seam, not through the SQL resource's own routes.

The session source mirrors :class:`~resourcey.sql.sql_resource.SqlResource`:
an explicit ``session_factory`` (the escape hatch) wins; otherwise a session
maker is resolved from ``session_manager`` by ``connection_name``, defaulting to
the process-wide :func:`~resourcey.sql.session_manager.get_sql_session_manager`.
The store is entered through the manifest's manager slot, so its resolution
happens while the manager is open.

This module imports no code outside the framework.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import PrivateAttr
from sqlalchemy import DateTime, LargeBinary, String, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.errors import ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.filestore.file_store import StoredObject, verify_upload
from resourcey.filestore.signed_url import SignedFileStore
from resourcey.sql.sql_resource import SqlResource
from resourcey.view.resource_view import ResourceView

if TYPE_CHECKING:
    from resourcey.sql.session_manager import SqlSessionManager

# The default table name; the app may override it per store.
DEFAULT_BLOB_TABLE = "file_blobs"


class FileBlobBase(DeclarativeBase):
    """The blob table's own declarative base."""


class FileBlob(FileBlobBase):
    """One row of stored file bytes plus the medium-side metadata."""

    __tablename__ = DEFAULT_BLOB_TABLE

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    content_type: Mapped[str | None] = mapped_column(String(255), default=None)
    checksum: Mapped[str | None] = mapped_column(String(64), default=None)
    etag: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _now())


def sql_file_blob_view(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    session_manager: SqlSessionManager | None = None,
    name: str | None = None,
    path: str = "file-blobs",
) -> Resource[Any, Any]:
    """A read-only, ``data``-hiding view of the blob table (debuggability only).

    Mirrors ``oauth_token_view``: the table is the single source of truth for
    the SQL medium, so exposing it read-only with the bytes projected away is
    useful for admin / debugging without risking the bytes leaking through a
    generated response. Register the **view**, not a bare
    ``SqlResource(FileBlob)`` -- registering both double-mounts the table.
    """
    inner: Resource[Any, Any] = SqlResource(
        FileBlob,
        path=path,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
    )
    return ResourceView(
        inner,
        exposed_field_overrides={
            "data": {
                "in_create_request": False,
                "in_create_response": False,
                "in_update_request": False,
                "in_update_response": False,
                "in_read_response": False,
                "in_search_response": False,
            }
        },
        exposed_actions=frozenset({Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}),
    )


class SqlFileStore(SignedFileStore):
    """A blob-table-backed medium.

    Attributes:
        connection_name: The connection resolved from ``session_manager``
            (default: its first connection).
        table_name: The blob table name (informational; the mapper owns the real
            table).
        signed_url_base_url: Prefix prepended to a framework-signed URL.
        signed_url_path_template: The route template the URL points at.
    """

    connection_name: str | None = None
    table_name: str = DEFAULT_BLOB_TABLE

    _session_factory: async_sessionmaker[AsyncSession] | None = PrivateAttr(default=None)
    _session_manager: SqlSessionManager | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        session_manager: SqlSessionManager | None = None,
        **data: Any,
    ) -> None:
        super().__init__(**data)
        self._session_factory = session_factory
        self._session_manager = session_manager

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> SqlFileStore:
        if self._session_factory is None:
            manager = self._session_manager
            if manager is None:
                from resourcey.sql.session_manager import get_sql_session_manager

                manager = get_sql_session_manager()
            self._session_factory = await manager.get_session_maker(self.connection_name)
        return self

    # -- medium operations ---------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        name: str | None = None,
        checksum: str | None = None,
        declared_size: int | None = None,
    ) -> StoredObject:
        verify_upload(data, declared_size=declared_size, checksum=checksum)
        etag = _etag(data)
        updated_at = _now()
        async with self._maker()() as session:
            existing = await session.get(FileBlob, key)
            if existing is None:
                session.add(
                    FileBlob(
                        key=key,
                        data=data,
                        name=name,
                        content_type=content_type,
                        checksum=checksum,
                        etag=etag,
                        updated_at=updated_at,
                    )
                )
            else:
                existing.data = data
                existing.name = name
                existing.content_type = content_type
                existing.checksum = checksum
                existing.etag = etag
                existing.updated_at = updated_at
            await session.commit()
        return StoredObject(
            key=key,
            size=len(data),
            name=name,
            content_type=content_type,
            checksum=checksum,
            etag=etag,
            updated_at=updated_at,
        )

    async def get(self, key: str) -> bytes | None:
        async with self._maker()() as session:
            row = await session.get(FileBlob, key)
            return None if row is None else bytes(row.data)

    async def head(self, key: str) -> StoredObject | None:
        async with self._maker()() as session:
            row = await session.get(FileBlob, key)
            return None if row is None else _to_stored(row)

    async def delete(self, key: str) -> None:
        async with self._maker()() as session:
            await session.execute(delete(FileBlob).where(FileBlob.key == key))
            await session.commit()

    async def list_objects(self, *, after: str | None = None, limit: int) -> list[StoredObject]:
        stmt = select(FileBlob).order_by(FileBlob.key).limit(limit)
        if after is not None:
            stmt = stmt.where(FileBlob.key > after)
        async with self._maker()() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_stored(row) for row in rows]

    async def count_objects(self) -> int:
        async with self._maker()() as session:
            result = await session.execute(select(func.count()).select_from(FileBlob))
            return int(result.scalar_one())

    # -- helpers --------------------------------------------------------

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise ResourceyConfigError(
                "SqlFileStore was used before entering it; register it in the manifest's "
                "managers so its session maker is resolved while the manager is open."
            )
        return self._session_factory

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession] | None:
        """The resolved session maker (the escape hatch), or ``None`` before entry."""
        return self._session_factory


def _now() -> datetime:
    return datetime.now(UTC)


def _etag(data: bytes) -> str:
    return f'"{hashlib.md5(data).hexdigest()}"'


def _to_stored(row: FileBlob) -> StoredObject:
    return StoredObject(
        key=row.key,
        size=len(row.data),
        name=row.name,
        content_type=row.content_type,
        checksum=row.checksum,
        etag=row.etag,
        updated_at=row.updated_at,
    )


async def create_blob_tables(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Create the blob table for an app that uses ``create_all`` rather than Alembic."""
    async with session_factory() as session:
        connection = await session.connection()
        await connection.run_sync(FileBlobBase.metadata.create_all)
