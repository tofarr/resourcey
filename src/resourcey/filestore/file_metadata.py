"""File **metadata** as a normal DTO-backed resource (issue #117).

File bytes live in a :class:`~resourcey.filestore.file_store.FileStore`; the
*metadata* -- name, size, MIME type, checksum, the medium's ETag, status, and the
opaque storage key -- is a normal SQLAlchemy-backed resource, so it gets the full
standard surface (create / read / update / delete / search / count / batch) plus
cache headers derived from the read model.

:class:`FileMetadata` is the conventional model; :func:`file_resource` serves it
with the server-owned ``key`` / ``status`` behaviour and object cleanup, and
:func:`~resourcey.filestore.file_routes.register_file_routes` mounts the
handshake. An app may instead declare its own metadata model and reuse the
handshake helpers.

Two guarantees this change builds in, as requested:

* **``updated_at``** is an ordinary column with ``default`` / ``onupdate``, so
  the DTO conventions make it framework-owned (write-once ``created_at``,
  re-set ``updated_at``), it appears in every response shape, and the read model
  carries it.
* **MIME type** (``content_type``) is a first-class column, supplied by the
  client on create and carried through the handshake.

The cache policy is overridden to a strong **ETag** over the projected bytes
(rather than the default last-modified) so every read carries a validator that
matches exactly what the response sends.

This module imports no code outside the framework.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.cache.cache_strategy import ETagCacheStrategy
from resourcey.core.dto import DtoField
from resourcey.core.errors import InvalidInputError
from resourcey.core.service import Service
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_store import FileStore
from resourcey.sql.sql_resource import SqlResource
from resourcey.sql.sql_service import SqlService

# The two file statuses: ``pending`` is minted-but-never-completed; ``ready`` is
# verified and downloadable. Plain strings keep the schema and wire explicit.
PENDING = "pending"
READY = "ready"

# Fields the handshake advances, never a client.
_SERVER_OWNED = DtoField(in_create_request=False, in_update_request=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FileMetadataBase(DeclarativeBase):
    """The metadata table's own declarative base."""


class FileMetadata(FileMetadataBase):
    """The conventional file-metadata row.

    ``key`` is server-assigned, opaque, and hidden from every response -- a
    client addresses a file by ``id``, never by its storage key. ``etag`` and
    ``status`` are advanced by the handshake. ``content_type`` is the client's
    declared MIME type and ``checksum`` an optional declared digest verified on
    completion.
    """

    __tablename__ = "files"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        info={
            "dto_field": DtoField(
                in_create_request=False,
                in_update_request=False,
                in_create_response=False,
                in_update_response=False,
                in_read_response=False,
                in_search_response=False,
            )
        },
    )
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(
        Integer, info={"dto_field": DtoField(in_update_request=False)}
    )
    checksum: Mapped[str | None] = mapped_column(String(64), default=None)
    etag: Mapped[str | None] = mapped_column(
        String(128), default=None, info={"dto_field": _SERVER_OWNED}
    )
    status: Mapped[str] = mapped_column(
        String(16), default=PENDING, info={"dto_field": _SERVER_OWNED}
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class FileMetadataService(SqlService[Any, Any]):
    """The metadata resource's service: server-owned key / status + object cleanup."""

    def __init__(
        self,
        resource: SqlResource[Any, Any],
        ctx: MutableMapping[Any, Any],
        session_factory: async_sessionmaker[AsyncSession],
        store: FileStore,
        max_size: int | None = None,
    ) -> None:
        super().__init__(resource, ctx, session_factory)
        self._store = store
        self._max_size = max_size

    async def create(self, payload: Any) -> Any:
        """Assign the opaque key + initial status, then insert the row."""
        declared = getattr(payload, "size", None)
        if self._max_size is not None and isinstance(declared, int) and declared > self._max_size:
            raise InvalidInputError(
                f"Declared size {declared} bytes exceeds the {self._max_size}-byte cap"
            )
        payload.key = uuid4().hex
        payload.status = PENDING
        return await super().create(payload)

    async def delete(self, id: Any) -> None:  # noqa: A002
        """Delete the metadata row and the stored object (so no orphan remains)."""
        record = await self.read(id)
        key = getattr(record, "key", None)
        await super().delete(id)
        if isinstance(key, str) and key:
            await self._store.delete(key)


class FileMetadataResource(SqlResource[Any, Any]):
    """``FileMetadata`` served with the key-assigning / object-cleaning service.

    Caching is overridden to a strong ETag over the projected bytes, so a read
    is validator-cacheable even though the read model also carries
    ``updated_at`` (the default strategy would pick last-modified).
    """

    def __init__(
        self,
        store: FileStore,
        *,
        path: str = "files",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        session_manager: Any = None,
        name: str | None = None,
        encryption_service: Any = None,
        max_size: int | None = None,
    ) -> None:
        super().__init__(
            FileMetadata,
            path=path,
            session_factory=session_factory,
            session_manager=session_manager,
            name=name,
            encryption_service=encryption_service,
        )
        self._store = store
        # Default the cap from the config block (env ``APP_MAX_SIZE``) so an
        # operator can bound uploads without touching the resource declaration.
        self._max_size = (
            max_size if max_size is not None else FileStoreConfig.get_instance().max_size
        )
        self._cache_strategy = ETagCacheStrategy()

    def make_service(
        self, ctx: MutableMapping[Any, Any], session_factory: async_sessionmaker[AsyncSession]
    ) -> Service[Any, Any]:
        return FileMetadataService(self, ctx, session_factory, self._store, self._max_size)

    def get_cache_strategy(self) -> Any:
        return self._cache_strategy


def file_resource(
    store: FileStore,
    *,
    path: str = "files",
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    session_manager: Any = None,
    name: str | None = None,
    max_size: int | None = None,
) -> FileMetadataResource:
    """Build the conventional ``files`` metadata resource over ``store``."""
    return FileMetadataResource(
        store,
        path=path,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        max_size=max_size,
    )
