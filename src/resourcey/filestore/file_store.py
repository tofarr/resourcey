"""``FileStore`` — the pluggable storage-medium seam for file bytes (issue #117, #158).

A ``FileStore`` moves file *bytes* against a medium (S3, an internal SQL blob
table, a local directory) while the API owns only *authorization*; there is no
separate metadata table and no tracked ``pending`` / ``ready`` status. **"Does
the medium have the bytes" is the only source of truth for a file's
existence**: ``read`` / ``search`` resolve directly against the medium's own
``head`` / ``list_objects``.

``create`` *is* the upload (case 2 of the upload-design discussion, not a
pre-signed-URL handshake): the client ``POST``s a ``multipart/form-data`` body
directly to the API (:mod:`resourcey.filestore.file_routes`), which streams it
straight into ``put`` -- ``name`` / ``content_type`` are read off the upload
itself and ``size`` / ``checksum`` are computed from the bytes actually
received, never a client declaration to verify. This means the bytes commit
synchronously inside the one request, so a wrapping
:class:`~resourcey.triggers.triggered_resource.TriggeredResource` fires its
``on_edit`` trigger only once the upload has genuinely landed, and the
generated ``201 Created`` (not a placeholder ``202``) is accurate. ``download``
is unchanged: :meth:`~FileStore.presign_get` still mints a short-lived
capability URL the client fetches directly.

``presign_get`` is **sync**: for S3 a pre-signed URL is a purely local SigV4
computation (no network round trip), and for SQL / local it is a local JWE
mint -- neither blocks the event loop. Only
``put`` / ``get`` / ``head`` / ``delete`` / ``list_objects`` / ``count_objects``
touch I/O and are async.

A store is its own async context manager and is entered through the manifest's
manager slot (``Manifest(resources=(...), managers=(store,))``), so its client
lifecycle is tied to the app exactly as a ``SqlSessionManager`` /
``MongoClientManager`` is.

This module imports no code outside the framework
and no optional driver (``boto3`` is imported lazily by
:mod:`resourcey.filestore.s3_file_store` only when a real client is built).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from resourcey.util.models import DiscriminatedUnionMixin


class StoredObject(BaseModel):
    """What a medium reports about one stored object (a ``head`` / ``list`` result).

    Attributes:
        key: The opaque storage key (also the resource's identifier).
        size: The object size in bytes.
        name: The client's declared file name, or ``None`` when the medium's
            listing call cannot report it cheaply (S3's ``ListObjectsV2``; a
            ``head`` always reports it).
        content_type: The MIME type the medium recorded, or ``None`` (subject
            to the same listing-cost caveat as ``name``).
        checksum: The client's declared digest, verified on upload, or ``None``.
        etag: The medium's own ETag (an S3 ETag, a content hash), or ``None``.
        updated_at: When the medium last wrote the object, or ``None``.
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

    Attributes:
        url: The capability URL the client fetches.
        method: The HTTP method to use (always ``GET``).
        expires_at: When the capability stops being usable.
        headers: Headers the client must echo (empty for a plain ``GET``).
    """

    url: str
    method: str
    expires_at: datetime
    headers: dict[str, str] = Field(default_factory=dict)


class FileStore(DiscriminatedUnionMixin, ABC):
    """The abstract storage-medium seam, discriminated by ``kind``.

    A concrete store declares its connection / bucket / root as Pydantic fields
    (so it is directly selectable from the environment through
    :class:`~resourcey.filestore.file_config.FileStoreConfig`'s ``LazyField``)
    and implements the medium operations. It is its own async context
    manager: enter it through the manifest's ``managers`` slot so its client /
    engine lifecycle is tied to the app.
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
        caller (read off the upload, or computed from ``data`` itself) --
        nothing here is a client declaration to verify.
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

        Objects are ordered ascending by key -- since a file's identifier *is*
        its storage key, this lines up with the framework's keyset-pagination-
        by-identifier model with no new cursor semantics. A listing call is not
        required to report ``name`` / ``content_type`` cheaply (S3's
        ``ListObjectsV2`` cannot); only ``head`` is.
        """

    @abstractmethod
    async def count_objects(self) -> int:
        """The total number of stored objects."""

    @abstractmethod
    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        """Mint a ``get`` capability URL for one object key (sync)."""
