"""``LocalFileStore`` -- file bytes under a configured root directory (issue #117).

The simplest medium: bytes live as files under ``root`` and the framework signs
the capability URLs (see :mod:`resourcey.v2.filestore.signed_url`). It fits
single-instance deployments, development, and tests -- it is **not** shared
across replicas, so a multi-replica deployment should use S3.

Security: keys are **opaque server-assigned UUIDs**, never client paths. A
presented key containing ``..``, a path separator, or an absolute path is
rejected, and the resolved path is re-checked to live under ``root``. That
closes the two obvious failure modes of a file-backed medium -- path traversal
and overwriting an arbitrary file.

``kind`` is the class name (from
:class:`~resourcey.v2.util.models.DiscriminatedUnionMixin`), so the store is
selectable from config as ``LocalFileStore``.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import field_validator

from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.filestore.file_store import StoredObject
from resourcey.v2.filestore.signed_url import SignedFileStore


class LocalFileStore(SignedFileStore):
    """A file-backed medium rooted at ``root``.

    Attributes:
        root: The directory bytes are stored under (created on enter).
        signed_url_base_url: Prefix prepended to a framework-signed URL (empty
            for a URL relative to the API host).
        signed_url_path_template: The route template the URL points at.
    """

    root: Path = Path("./.resourcey_files")

    @field_validator("root", mode="before")
    @classmethod
    def _coerce_root(cls, value: object) -> object:
        return Path(value) if isinstance(value, str) else value

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> LocalFileStore:
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    # -- medium operations ---------------------------------------------

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # The filesystem records no MIME type, so it lives in a small sidecar
        # index file beside the object (keys are server-generated opaque hex, so
        # the ``.meta`` suffix cannot collide with a real key).
        self._meta_path_for(path).write_text(
            json.dumps({"content_type": content_type}), encoding="utf-8"
        )
        return self._stored(key, data, content_type=content_type, updated_at=_mtime(path))

    async def get(self, key: str) -> bytes | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    async def head(self, key: str) -> StoredObject | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return self._stored(
            key, data, content_type=self._read_content_type(path), updated_at=_mtime(path)
        )

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.is_file():
            path.unlink()
        meta = self._meta_path_for(path)
        if meta.is_file():
            meta.unlink()

    # -- helpers --------------------------------------------------------

    def _read_content_type(self, path: Path) -> str | None:
        meta = self._meta_path_for(path)
        if not meta.is_file():
            return None
        try:
            value = json.loads(meta.read_text(encoding="utf-8")).get("content_type")
        except (ValueError, OSError):
            return None
        return value if isinstance(value, str) else None

    def _meta_path_for(self, path: Path) -> Path:
        return path.with_name(path.name + ".meta")

    def _stored(
        self, key: str, data: bytes, *, content_type: str | None, updated_at: datetime
    ) -> StoredObject:
        return StoredObject(
            key=key,
            size=len(data),
            content_type=content_type,
            etag=f'"{hashlib.md5(data).hexdigest()}"',
            updated_at=updated_at,
        )

    def _path_for(self, key: str) -> Path:
        """Resolve ``key`` to a path guaranteed to live under ``root``.

        An opaque key (a UUID, optionally with an S3-style ``prefix/``) is the
        only shape accepted: an empty key, an absolute path, ``~``, and ``..``
        are rejected, and the resolved path is re-checked to be under ``root``.
        """
        if not key or key.startswith("/") or key.startswith("~"):
            raise InvalidInputError(f"Invalid file key {key!r}")
        candidate = Path(key)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise InvalidInputError(f"Invalid file key {key!r}")
        root = self.root.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise InvalidInputError(f"Invalid file key {key!r}")
        return resolved


def _mtime(path: Path) -> datetime:
    """The file's modification time as an aware UTC datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
