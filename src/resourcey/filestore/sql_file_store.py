"""``SqlFileStore`` -- file bytes in an internal blob table (issue #117).

For deployments that already run Postgres and want no second system: the bytes
live in a dedicated ``file_blobs`` table, accessed through SQLAlchemy directly
(the documented escape hatch), and the framework signs the capability URLs
(:mod:`resourcey.filestore.signed_url`). It suits small files; large files
belong on S3.

The blob table is **storage, not a resource**: it is never registered in a
``Manifest`` and never derives a DTO, so its bytes can never leak through a
generated read model. The app owns the schema (Alembic / ``create_all`` against
:data:`FileBlobBase`), exactly as it owns the tables its resources serve.

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
from sqlalchemy import DateTime, LargeBinary, String, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.errors import ResourceyConfigError
from resourcey.filestore.file_store import StoredObject
from resourcey.filestore.signed_url import SignedFileStore

if TYPE_CHECKING:
    from resourcey.sql.session_manager import SqlSessionManager

# The default table name; the app may override it per store.
DEFAULT_BLOB_TABLE = "file_blobs"


class FileBlobBase(DeclarativeBase):
    """The blob table's own declarative base -- internal storage, not a resource."""


class FileBlob(FileBlobBase):
    """One row of stored file bytes plus the medium-side metadata."""

    __tablename__ = DEFAULT_BLOB_TABLE

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str | None] = mapped_column(String(255), default=None)
    etag: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _now())


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

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        etag = _etag(data)
        updated_at = _now()
        async with self._maker()() as session:
            existing = await session.get(FileBlob, key)
            if existing is None:
                session.add(
                    FileBlob(
                        key=key,
                        data=data,
                        content_type=content_type,
                        etag=etag,
                        updated_at=updated_at,
                    )
                )
            else:
                existing.data = data
                existing.content_type = content_type
                existing.etag = etag
                existing.updated_at = updated_at
            await session.commit()
        return StoredObject(
            key=key,
            size=len(data),
            content_type=content_type,
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
            if row is None:
                return None
            return StoredObject(
                key=row.key,
                size=len(row.data),
                content_type=row.content_type,
                etag=row.etag,
                updated_at=row.updated_at,
            )

    async def delete(self, key: str) -> None:
        async with self._maker()() as session:
            await session.execute(delete(FileBlob).where(FileBlob.key == key))
            await session.commit()

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


async def create_blob_tables(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Create the blob table for an app that uses ``create_all`` rather than Alembic."""
    async with session_factory() as session:
        connection = await session.connection()
        await connection.run_sync(FileBlobBase.metadata.create_all)
