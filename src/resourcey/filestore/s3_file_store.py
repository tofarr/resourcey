"""``S3FileStore`` -- file bytes in an S3 bucket, uploaded through the API (issue #117, #158).

The production medium. ``create`` is a direct ``multipart/form-data`` upload
to the API (case 2 of the upload-design discussion, not a pre-signed-URL
handshake), so the API process proxies the bytes to S3 via ``put_object`` --
the trade-off deliberately made in exchange for the bytes genuinely existing
by the time ``create`` returns (so a :class:`~resourcey.triggers.triggered_resource.TriggeredResource`
fires only after a real upload). ``presign_get`` stays a purely local SigV4
computation -- no network round trip -- so **download** still transfers
directly against S3, unaffected by this trade-off.

``name`` / ``checksum`` are stored as S3 object **user metadata**
(``x-amz-meta-*``), readable only via ``HeadObject`` (i.e. only through
``head``), never through ``ListObjectsV2`` (``list_objects``), which is why
``list_objects`` below reports ``name`` / ``content_type`` / ``checksum`` as
``None``: it is the cheapest common contract across the three media, made
explicit rather than letting this medium be incidentally richer than it can
actually list.

A single ``put_object`` call still means the upload is bound by
``S3_MAX_PUT_BYTES`` (5 GiB, S3's own single-``PUT`` cap); streaming the
upload straight into S3's multipart-upload API (so neither this process nor
S3 ever needs the whole object in memory at once) is valuable future work,
deliberately out of scope here.

``boto3`` (and the ``s3`` extra, ``resourcey[s3]``) is imported **lazily**,
only when a real client is built, so ``filestore`` imports cleanly without
it. An explicit ``client=`` is the escape hatch: tests and callers may inject a
stub exposing ``put_object`` / ``get_object`` / ``head_object`` /
``delete_object`` / ``list_objects_v2`` / ``generate_presigned_url``.

This module imports no code outside the framework.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from pydantic import PrivateAttr

from resourcey.core.errors import ResourceyConfigError
from resourcey.encryption.encryption_service import utc_now
from resourcey.filestore.file_store import FileStore, PresignedUrl, StoredObject

# The action name boto3 generates pre-signed URLs for.
_GET_ACTION = "get_object"

# S3's single-``PUT`` object cap; multipart uploads are out of scope.
S3_MAX_PUT_BYTES = 5 * 1024 * 1024 * 1024

# The ``x-amz-meta-*`` keys ``name`` / ``checksum`` are carried under.
_META_NAME = "name"
_META_CHECKSUM = "checksum"


class S3FileStore(FileStore):
    """An S3-backed medium with native SigV4 pre-signed URLs.

    Attributes:
        bucket: The bucket bytes are stored in.
        region: The bucket's region (optional; boto3's own resolution applies).
        endpoint_url: A custom endpoint (e.g. MinIO); optional.
        prefix: A key prefix (an S3-style partition) prepended to every key.
        access_key_id / secret_access_key: Optional explicit credentials.
    """

    bucket: str
    region: str | None = None
    endpoint_url: str | None = None
    prefix: str = ""
    access_key_id: str | None = None
    secret_access_key: str | None = None

    _client: Any = PrivateAttr(default=None)

    def __init__(self, *, client: Any = None, **data: Any) -> None:
        super().__init__(**data)
        self._client = client

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> S3FileStore:
        self._get_client()
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
    ) -> StoredObject:
        if len(data) > S3_MAX_PUT_BYTES:
            raise ResourceyConfigError(
                f"{len(data)} bytes exceeds S3's single-PUT cap of {S3_MAX_PUT_BYTES} "
                "(multipart upload is out of scope)."
            )
        client = self._get_client()
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._object_key(key), "Body": data}
        if content_type is not None:
            kwargs["ContentType"] = content_type
        metadata = _metadata_fields(name, checksum)
        if metadata:
            kwargs["Metadata"] = metadata
        await asyncio.to_thread(client.put_object, **kwargs)
        return await self._stored_after_write(key, len(data), name, content_type, checksum)

    async def get(self, key: str) -> bytes | None:
        client = self._get_client()
        try:
            response = await asyncio.to_thread(
                client.get_object, Bucket=self.bucket, Key=self._object_key(key)
            )
        except client.exceptions.NoSuchKey:
            return None
        body = response["Body"]
        return await asyncio.to_thread(body.read)

    async def head(self, key: str) -> StoredObject | None:
        client = self._get_client()
        try:
            response = await asyncio.to_thread(
                client.head_object, Bucket=self.bucket, Key=self._object_key(key)
            )
        except client.exceptions.ClientError:
            return None
        metadata = response.get("Metadata") or {}
        return StoredObject(
            key=key,
            size=response.get("ContentLength", 0),
            name=metadata.get(_META_NAME),
            content_type=response.get("ContentType"),
            checksum=metadata.get(_META_CHECKSUM),
            etag=response.get("ETag"),
            updated_at=response.get("LastModified"),
        )

    async def delete(self, key: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(client.delete_object, Bucket=self.bucket, Key=self._object_key(key))

    async def list_objects(self, *, after: str | None = None, limit: int) -> list[StoredObject]:
        """List via ``ListObjectsV2``, ascending by key (S3's native order).

        ``name`` / ``content_type`` / ``checksum`` are reported as ``None``:
        ``ListObjectsV2`` genuinely cannot return per-object user metadata (only
        ``HeadObject`` can, at one call per key), so a list result here matches
        every other medium's *cheapest common contract* rather than this one
        being incidentally richer.
        """
        client = self._get_client()
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "MaxKeys": limit}
        if self.prefix:
            kwargs["Prefix"] = f"{self.prefix.strip('/')}/"
        if after is not None:
            kwargs["StartAfter"] = self._object_key(after)
        response = await asyncio.to_thread(client.list_objects_v2, **kwargs)
        results = []
        for obj in response.get("Contents", []):
            object_key = obj["Key"]
            key = object_key[len(kwargs.get("Prefix", "")) :] if self.prefix else object_key
            results.append(
                StoredObject(
                    key=key,
                    size=obj.get("Size", 0),
                    etag=obj.get("ETag"),
                    updated_at=obj.get("LastModified"),
                )
            )
        return results

    async def count_objects(self) -> int:
        client = self._get_client()
        kwargs: dict[str, Any] = {"Bucket": self.bucket}
        if self.prefix:
            kwargs["Prefix"] = f"{self.prefix.strip('/')}/"
        total = 0
        continuation: str | None = None
        while True:
            if continuation is not None:
                kwargs["ContinuationToken"] = continuation
            response = await asyncio.to_thread(client.list_objects_v2, **kwargs)
            total += int(response.get("KeyCount", len(response.get("Contents", []))))
            if not response.get("IsTruncated"):
                return total
            continuation = response.get("NextContinuationToken")

    # -- native pre-signed capability -----------------------------------

    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        url = self._presign(
            _GET_ACTION, {"Bucket": self.bucket, "Key": self._object_key(key)}, expires_in_seconds
        )
        return PresignedUrl(
            url=url,
            method="GET",
            expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
        )

    # -- helpers --------------------------------------------------------

    def _presign(self, action: str, params: dict[str, Any], expires_in_seconds: int) -> str:
        client = self._get_client()
        url: str = client.generate_presigned_url(
            action, Params=params, ExpiresIn=expires_in_seconds
        )
        return url

    async def _stored_after_write(
        self,
        key: str,
        size: int,
        name: str | None,
        content_type: str | None,
        checksum: str | None,
    ) -> StoredObject:
        """Describe the object just written, preferring S3's own head metadata."""
        reported = await self.head(key)
        if reported is not None:
            return reported
        return StoredObject(
            key=key,
            size=size,
            name=name,
            content_type=content_type,
            checksum=checksum,
            updated_at=utc_now(),
        )

    def _object_key(self, key: str) -> str:
        """The S3 object key: the store's ``prefix`` mounted over the opaque key."""
        if not self.prefix:
            return key
        return f"{self.prefix.strip('/')}/{key}"

    def _get_client(self) -> Any:
        """The S3 client, building one lazily and raising an actionable error without ``boto3``."""
        if self._client is None:
            self._client = _build_client(self)
        return self._client

    @property
    def client(self) -> Any:
        """The underlying S3 client (the escape hatch), or ``None`` until built."""
        return self._client


def _metadata_fields(name: str | None, checksum: str | None) -> dict[str, str]:
    """The ``x-amz-meta-*`` fields for ``name`` / ``checksum`` (only the set ones)."""
    fields: dict[str, str] = {}
    if name is not None:
        fields[_META_NAME] = name
    if checksum is not None:
        fields[_META_CHECKSUM] = checksum
    return fields


def _build_client(store: S3FileStore) -> Any:
    """Build a boto3 S3 client, or raise an actionable ``ImportError`` naming the extra."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError("S3FileStore requires boto3; install it with 'resourcey[s3]'.") from exc
    kwargs: dict[str, Any] = {}
    if store.region is not None:
        kwargs["region_name"] = store.region
    if store.endpoint_url is not None:
        kwargs["endpoint_url"] = store.endpoint_url
    if store.access_key_id is not None and store.secret_access_key is not None:
        kwargs["aws_access_key_id"] = store.access_key_id
        kwargs["aws_secret_access_key"] = store.secret_access_key
    return boto3.client("s3", **kwargs)
