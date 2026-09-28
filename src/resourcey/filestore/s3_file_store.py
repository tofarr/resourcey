"""``S3FileStore`` -- file bytes in an S3 bucket with native pre-signed URLs (issue #117).

The production medium. Put / get / head / delete use the S3 client (the
server-side fallback), while ``presign_put`` / ``presign_get`` are a purely
local SigV4 computation -- no network round trip and no blocking of the event
loop -- so the client transfers directly against S3 and the API only mints the
capability.

``boto3`` (and the ``s3`` extra, ``resourcey[s3]``) is imported **lazily**,
only when a real client is built, so ``filestore`` imports cleanly without
it. An explicit ``client=`` is the escape hatch: tests and callers may inject a
stub exposing ``put_object`` / ``get_object`` / ``head_object`` /
``delete_object`` / ``generate_presigned_url``.

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

# The action names boto3 generates pre-signed URLs for.
_PUT_ACTION = "put_object"
_GET_ACTION = "get_object"

# S3's single-``PUT`` object cap; multipart uploads are out of scope.
S3_MAX_PUT_BYTES = 5 * 1024 * 1024 * 1024


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

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        if len(data) > S3_MAX_PUT_BYTES:
            raise ResourceyConfigError(
                f"{len(data)} bytes exceeds S3's single-PUT cap of {S3_MAX_PUT_BYTES} "
                "(multipart upload is out of scope)."
            )
        client = self._get_client()
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._object_key(key), "Body": data}
        if content_type is not None:
            kwargs["ContentType"] = content_type
        await asyncio.to_thread(client.put_object, **kwargs)
        return await self._stored_after_write(key, len(data), content_type)

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
        return StoredObject(
            key=key,
            size=response.get("ContentLength", 0),
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
            updated_at=response.get("LastModified"),
        )

    async def delete(self, key: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(client.delete_object, Bucket=self.bucket, Key=self._object_key(key))

    # -- native pre-signed capabilities --------------------------------

    def presign_put(
        self, key: str, *, content_type: str | None, expires_in_seconds: int
    ) -> PresignedUrl:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": self._object_key(key)}
        headers: dict[str, str] = {}
        if content_type is not None:
            # The content type is baked into the signature, so the client must
            # echo it on the transfer.
            params["ContentType"] = content_type
            headers["Content-Type"] = content_type
        url = self._presign(_PUT_ACTION, params, expires_in_seconds)
        return PresignedUrl(
            url=url,
            method="PUT",
            expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
            headers=headers,
        )

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
        self, key: str, size: int, content_type: str | None
    ) -> StoredObject:
        """Describe the object just written, preferring S3's own head metadata."""
        reported = await self.head(key)
        if reported is not None:
            return reported
        return StoredObject(key=key, size=size, content_type=content_type, updated_at=utc_now())

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
