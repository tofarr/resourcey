"""``FileStore`` — the pluggable storage-medium seam for file bytes (issue #117, #158).

A ``FileStore`` moves opaque file *bytes* against a medium (S3, an internal SQL
blob table, a local directory) while the API owns only *authorization*; there is
no separate metadata table and no tracked ``pending`` / ``ready`` status. **"Does
the medium have the bytes" is the only source of truth for a file's existence**:
``read`` / ``search`` resolve directly against the medium's own
``head`` / ``list_objects``, so a file simply does not exist until its bytes do,
and appears the moment they land -- with no additional client call, and no
orphan-row problem, across all three media uniformly (including S3, where a
client uploads straight to the bucket and nothing else tells the API the bytes
arrived).

``create`` therefore only *allocates* an opaque key and mints an upload
capability (a ``PUT`` for the local / SQL media, a presigned ``POST`` for S3);
nothing is persisted until the upload lands. The capability carries the
client's declared ``name`` / ``content_type`` / ``size`` / ``checksum`` as
**signed claims**, verified against the uploaded bytes before they are
committed (:func:`verify_upload`) -- a mismatch is simply rejected, with no
``failed`` status to track, and the client may retry against the same
capability until it expires.

``presign_upload`` / ``presign_get`` are **sync**: for S3 a pre-signed URL/POST
is a purely local SigV4 computation (no network round trip), and for SQL /
local it is a local JWE mint -- neither blocks the event loop. Only
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

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from resourcey.core.errors import ConflictError
from resourcey.util.models import DiscriminatedUnionMixin

# The two capability operations a signed URL can be bound to. Plain strings
# because they are serialized into the JWE claim dict.
PUT_OPERATION = "put"
GET_OPERATION = "get"


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


class UploadCapability(BaseModel):
    """A short-lived upload capability, minted by ``create``.

    Attributes:
        url: The URL the client transfers against.
        method: ``PUT`` (local / SQL media -- the client sends raw bytes) or
            ``POST`` (S3 -- a presigned POST policy, the client sends a
            multipart form).
        fields: Extra form fields the client must include with a ``POST``
            (S3's policy conditions -- key, content-type, declared size,
            metadata). Empty for a ``PUT``.
        headers: Headers the client must echo on a ``PUT`` (e.g.
            ``Content-Type``, baked into the signature). Empty for a ``POST``.
        expires_at: When the capability stops being usable.
    """

    url: str
    method: str
    fields: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime


def verify_upload(
    data: bytes, *, declared_size: int | None = None, checksum: str | None = None
) -> None:
    """Reject ``data`` that does not match the claims it was uploaded under.

    Shared by the local and SQL media (which receive the bytes themselves, so
    they can verify before committing); S3 enforces the same conditions
    natively via its presigned-POST policy. Raises
    :class:`~resourcey.core.errors.ConflictError` (mapped to ``409``) on a
    mismatch -- there is no ``failed`` status to track, the client simply
    retries against the same (still-valid) capability.
    """
    if declared_size is not None and len(data) != declared_size:
        raise ConflictError(f"Uploaded object is {len(data)} bytes, expected {declared_size}")
    if checksum is not None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != checksum:
            raise ConflictError(f"Uploaded object checksum {actual} does not match {checksum}")


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
        declared_size: int | None = None,
    ) -> StoredObject:
        """Write ``data`` under ``key`` and return what the medium recorded.

        ``declared_size`` / ``checksum``, when given, are verified against
        ``data`` (:func:`verify_upload`) before the write is committed.
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
    def presign_upload(
        self,
        key: str,
        *,
        name: str | None,
        content_type: str | None,
        size: int | None,
        checksum: str | None,
        expires_in_seconds: int,
    ) -> UploadCapability:
        """Mint an upload capability for one object key (sync).

        The declared ``name`` / ``content_type`` / ``size`` / ``checksum``
        travel with the capability (signed claims for the local / SQL media,
        policy conditions + metadata fields for S3) so the upload can be
        verified against them before it is visible.
        """

    @abstractmethod
    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        """Mint a ``get`` capability URL for one object key (sync)."""
