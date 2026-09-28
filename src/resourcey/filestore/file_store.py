"""``FileStore`` — the pluggable storage-medium seam for file bytes (issue #117).

A ``FileStore`` moves opaque file *bytes* against a medium (S3, an internal SQL
blob table, a local directory) while the API owns only *metadata* and
*authorization*. On the happy path the bytes never pass through a request
handler: the API mints a short-lived capability URL bound to one operation
(``put`` or ``get``) on one opaque key, and the client transfers directly
against it.

The same ``put`` / ``get`` / ``head`` / ``delete`` + ``presign_put`` /
``presign_get`` contract covers every medium, so the medium is a *deployment*
choice and the client handshake is identical: an S3 store returns a native
SigV4 URL, while the SQL / local stores return a framework-signed URL served by
:mod:`resourcey.filestore.file_routes`.

``presign_*`` are **sync**: for S3 a pre-signed URL is a purely local SigV4
computation (no network round trip), and for SQL / local it is a local JWE
mint -- neither blocks the event loop. Only ``put`` / ``get`` / ``head`` /
``delete`` touch I/O and are async.

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

# The two capability operations a signed URL can be bound to. Plain strings
# because they are serialized into the JWE claim dict.
PUT_OPERATION = "put"
GET_OPERATION = "get"


class StoredObject(BaseModel):
    """What a medium reports about one stored object (a ``head`` result).

    Attributes:
        key: The opaque storage key.
        size: The object size in bytes.
        content_type: The MIME type the medium recorded, or ``None``.
        etag: The medium's own ETag (an S3 ETag, a content hash), or ``None``.
        updated_at: When the medium last wrote the object, or ``None``.
    """

    key: str
    size: int
    content_type: str | None = None
    etag: str | None = None
    updated_at: datetime | None = None


class PresignedUrl(BaseModel):
    """A short-lived capability URL, identical in shape across every medium.

    Attributes:
        url: The capability URL the client transfers against.
        method: The HTTP method to use (``PUT`` / ``GET``).
        expires_at: When the capability stops being usable.
        headers: Headers the client must echo on the transfer (e.g. S3
            ``Content-Type``, which is baked into the signature).
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
    and implements the six medium operations. It is its own async context
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
    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        """Write ``data`` under ``key`` and return what the medium recorded."""

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
    def presign_put(
        self, key: str, *, content_type: str | None, expires_in_seconds: int
    ) -> PresignedUrl:
        """Mint a ``put`` capability URL for one object key (sync)."""

    @abstractmethod
    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        """Mint a ``get`` capability URL for one object key (sync)."""
