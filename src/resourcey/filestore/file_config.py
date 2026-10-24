"""``FileStoreConfig`` -- the env-driven medium selection and transfer limits (issue #117).

The medium is selected with no code change through a
:class:`~resourcey.config.lazy_field.LazyField`: ``MEDIUM_CLASS`` names a
:class:`~resourcey.filestore.file_store.FileStore` subclass by dotted path, and
its own fields then parse under the ``MEDIUM_`` prefix. With ``MEDIUM_CLASS``
unset the config falls back to
:class:`~resourcey.filestore.local_file_store.LocalFileStore`.

The download TTL and size cap are ordinary fields under the process-wide prefix
(``APP_DOWNLOAD_URL_TTL_SECONDS`` / ``APP_MAX_SIZE``). There is no upload TTL:
``create`` is a direct upload, not a capability to use later. The ``*_seconds``
spelling keeps the TTL env-parseable (the parser has no ``timedelta``).

This module imports no code outside the framework.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.filestore.file_store import FileStore
from resourcey.filestore.local_file_store import LocalFileStore

# Short, because a signed URL is a bearer token.
DEFAULT_DOWNLOAD_URL_TTL_SECONDS = 15 * 60


class FileStoreConfig(BaseConfig):
    """The file store's config block: the selected medium plus transfer limits.

    ``medium`` is resolved lazily from ``MEDIUM_CLASS`` (default
    :class:`LocalFileStore`); ``max_size`` optionally caps an upload's size.
    """

    medium: ClassVar[FileStore] = LazyField(default=LocalFileStore)  # type: ignore[assignment]
    download_url_ttl_seconds: int = DEFAULT_DOWNLOAD_URL_TTL_SECONDS
    max_size: int | None = None

    @property
    def download_url_ttl(self) -> timedelta:
        """The ``get`` capability TTL as a ``timedelta``."""
        return timedelta(seconds=self.download_url_ttl_seconds)
