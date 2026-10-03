"""``FileStoreConfig`` -- the env-driven medium selection and transfer limits (issue #117).

The medium is selected with no code change through a
:class:`~resourcey.config.lazy_field.LazyField` (the exact mechanism the
``DependencyBuilder`` seam uses): ``MEDIUM_CLASS`` names a
:class:`~resourcey.filestore.file_store.FileStore` subclass by dotted path,
and the selected medium is a Pydantic model so its own fields parse from the
environment under the ``MEDIUM_`` prefix (e.g. ``MEDIUM_ROOT`` for a
``LocalFileStore``). With ``MEDIUM_CLASS`` unset the config falls back to
:class:`~resourcey.filestore.local_file_store.LocalFileStore`, so tests and
local development work out of the box.

The download TTL and size cap are ordinary fields under the process-wide
prefix (``APP_DOWNLOAD_URL_TTL_SECONDS`` / ``APP_MAX_SIZE``). There is no
upload TTL: ``create`` is a direct upload, not a capability to use later, so
there is nothing to expire. The ``*_seconds`` spelling keeps the TTL
env-parseable (the parser has no ``timedelta``), and ``download_url_ttl``
exposes it as a ``timedelta`` for call sites that prefer it.

This module imports no code outside the framework.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.filestore.file_store import FileStore
from resourcey.filestore.local_file_store import LocalFileStore

# The default download-capability TTL: short, because a signed URL is a bearer token.
DEFAULT_DOWNLOAD_URL_TTL_SECONDS = 15 * 60


class FileStoreConfig(BaseConfig):
    """The file store's config block: the selected medium plus transfer limits.

    Attributes:
        medium: The selected medium instance, resolved lazily from
            ``MEDIUM_CLASS`` (default :class:`LocalFileStore`).
        download_url_ttl_seconds: How long a ``get`` capability stays valid.
        max_size: An optional cap on an upload's size, in bytes.
    """

    medium: ClassVar[FileStore] = LazyField(default=LocalFileStore)  # type: ignore[assignment]
    download_url_ttl_seconds: int = DEFAULT_DOWNLOAD_URL_TTL_SECONDS
    max_size: int | None = None

    @property
    def download_url_ttl(self) -> timedelta:
        """The ``get`` capability TTL as a ``timedelta``."""
        return timedelta(seconds=self.download_url_ttl_seconds)
