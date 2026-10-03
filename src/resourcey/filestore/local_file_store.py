"""``LocalFileStore`` -- file bytes under a configured root directory (issue #117, #158).

The simplest medium: bytes live as files under ``root`` and the framework signs
the capability URLs (see :mod:`resourcey.filestore.signed_url`). It fits
single-instance deployments, development, and tests -- it is **not** shared
across replicas, so a multi-replica deployment should use S3.

Security: keys are **opaque server-assigned UUIDs**, never client paths. A
presented key containing ``..``, a path separator, or an absolute path is
rejected, and the resolved path is re-checked to live under ``root``. That
closes the two obvious failure modes of a file-backed medium -- path traversal
and overwriting an arbitrary file.

``put`` writes to a **temp file in the same directory, then ``os.replace()``s
it into place**: writing the final path directly and the sidecar in a second
step is not atomic, so a concurrent reader could otherwise observe a truncated
file or stale metadata mid-upload. ``name`` / ``content_type`` / ``checksum``
fold into the one sidecar JSON, written atomically with the rename, so they
become visible at the same instant the bytes do -- exactly when ``head`` /
``get`` first see the key, which is this medium's half of "existence is the
only state".

``kind`` is the class name (from
:class:`~resourcey.util.models.DiscriminatedUnionMixin`), so the store is
selectable from config as ``LocalFileStore``.

This module imports no code outside the framework.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import field_validator

from resourcey.core.errors import InvalidInputError
from resourcey.filestore.file_store import StoredObject
from resourcey.filestore.signed_url import SignedFileStore


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

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        name: str | None = None,
        checksum: str | None = None,
    ) -> StoredObject:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes + sidecar to temp files in the same directory, then
        # ``os.replace()`` each into place -- a concurrent reader therefore
        # never observes a truncated file or metadata that disagrees with it.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        meta_path = self._meta_path_for(path)
        meta_fd, meta_tmp_name = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(meta_fd, "w", encoding="utf-8") as tmp:
                tmp.write(
                    json.dumps({"name": name, "content_type": content_type, "checksum": checksum})
                )
            os.replace(meta_tmp_name, meta_path)
        except BaseException:
            Path(meta_tmp_name).unlink(missing_ok=True)
            raise
        return self._stored(
            key, data, name=name, content_type=content_type, checksum=checksum, path=path
        )

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
        meta = self._read_meta(path)
        return self._stored(
            key,
            data,
            name=meta.get("name"),
            content_type=meta.get("content_type"),
            checksum=meta.get("checksum"),
            path=path,
        )

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.is_file():
            path.unlink()
        meta = self._meta_path_for(path)
        if meta.is_file():
            meta.unlink()

    async def list_objects(self, *, after: str | None = None, limit: int) -> list[StoredObject]:
        """List the objects directly under ``root``, ordered ascending by key.

        Only top-level keys are listed (the server-generated keys this medium
        serves are flat hex strings; a caller who writes a nested key via
        ``put`` directly can still ``get`` / ``head`` / ``delete`` it, but it
        will not appear in a listing).
        """
        if not self.root.is_dir():
            return []
        keys = sorted(
            p.name for p in self.root.iterdir() if p.is_file() and not p.name.endswith(".meta")
        )
        if after is not None:
            keys = [k for k in keys if k > after]
        results = []
        for key in keys[:limit]:
            found = await self.head(key)
            if found is not None:
                results.append(found)
        return results

    async def count_objects(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(1 for p in self.root.iterdir() if p.is_file() and not p.name.endswith(".meta"))

    # -- helpers --------------------------------------------------------

    def _read_meta(self, path: Path) -> dict[str, str | None]:
        meta = self._meta_path_for(path)
        if not meta.is_file():
            return {}
        try:
            loaded = json.loads(meta.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _meta_path_for(self, path: Path) -> Path:
        return path.with_name(path.name + ".meta")

    def _stored(
        self,
        key: str,
        data: bytes,
        *,
        name: str | None,
        content_type: str | None,
        checksum: str | None,
        path: Path,
    ) -> StoredObject:
        return StoredObject(
            key=key,
            size=len(data),
            name=name,
            content_type=content_type,
            checksum=checksum,
            etag=f'"{hashlib.md5(data).hexdigest()}"',
            updated_at=_mtime(path),
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
