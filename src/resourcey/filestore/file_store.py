"""``FileStore`` — the pluggable storage-medium seam for file bytes (issue #117, #158).

A ``FileStore`` moves file *bytes* against a medium (S3, an internal SQL blob
table, a local directory) while the API owns only *authorization*. There is no
metadata table and no ``pending`` / ``ready`` status: **"does the medium have the
bytes" is the only source of truth for a file's existence**, so ``read`` /
``search`` resolve directly against the medium's own ``head`` / ``list_objects``.

``create`` *is* the upload: the client ``POST``s a ``multipart/form-data`` body
directly to the API, which streams it straight into ``put``. The bytes therefore
commit synchronously inside the one request, so a wrapping
:class:`~resourcey.triggers.triggered_resource.TriggeredResource` fires only once
the upload has genuinely landed. ``download`` mints a short-lived capability URL
through :meth:`~FileStore.presign_get`.

``presign_get`` is **sync** — for S3 a purely local SigV4 computation, for SQL /
local a local JWE mint — so it never blocks the event loop.

A store is its own async context manager, entered through the manifest's manager
slot so its client lifecycle is tied to the app.

This module imports no code outside the framework and no optional driver
(``boto3`` is imported lazily by :mod:`resourcey.filestore.s3_file_store`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from resourcey.util.models import DiscriminatedUnionMixin


class StoredObject(BaseModel):
    """What a medium reports about one stored object (a ``head`` / ``list`` result).

    ``name`` / ``content_type`` / ``checksum`` are ``None`` from a listing call
    that cannot report them cheaply (S3's ``ListObjectsV2``); ``head`` always
    reports them.
    """

    key: str
    size: int
    name: str | None = None
    content_type: str | None = None
    checksum: str | None = None
    etag: str | None = None
    updated_at: datetime | None = None


class PresignedUrl(BaseModel):
    """A short-lived ``GET`` capability URL, identical in shape across every medium.

    ``headers`` are the headers the client must echo (empty for a plain ``GET``).
    """

    url: str
    method: str
    expires_at: datetime
    headers: dict[str, str] = Field(default_factory=dict)


class FileStore(DiscriminatedUnionMixin, ABC):
    """The abstract storage-medium seam, discriminated by ``kind``.

    A concrete store declares its connection / bucket / root as Pydantic fields
    (so it is selectable from the environment through
    :class:`~resourcey.filestore.file_config.FileStoreConfig`'s ``LazyField``).
    """

    async def __aenter__(self) -> FileStore:
        """Open the medium's client / engine (override to do real work)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the medium's client / engine (override to do real work)."""
        return None

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        name: str | None = None,
        checksum: str | None = None,
    ) -> StoredObject:
        """Write ``data`` under ``key`` and return what the medium recorded.

        ``name`` / ``content_type`` / ``checksum`` are already resolved by the
        caller, never a client declaration to verify.
        """

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """Read ``key``'s bytes, or ``None`` when the object is absent."""

    @abstractmethod
    async def head(self, key: str) -> StoredObject | None:
        """Report ``key``'s metadata without transferring its bytes, or ``None``."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete ``key`` (a no-op when it is already absent)."""

    @abstractmethod
    async def list_objects(self, *, after: str | None = None, limit: int) -> list[StoredObject]:
        """List up to ``limit`` objects with key strictly greater than ``after``.

        Objects are ordered ascending by key, which lines up with the
        keyset-pagination-by-identifier model since a file's identifier *is* its
        storage key.
        """

    @abstractmethod
    async def count_objects(self) -> int:
        """The total number of stored objects."""

    @abstractmethod
    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        """Mint a ``get`` capability URL for one object key (sync)."""
