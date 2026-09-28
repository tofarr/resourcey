"""``FileStoreConfig`` -- the env-driven medium selection and transfer limits (issue #117).

The medium is selected with no code change through a
:class:`~resourcey.v2.config.lazy_field.LazyField` (the exact mechanism the
``DependencyBuilder`` seam uses): ``MEDIUM_CLASS`` names a
:class:`~resourcey.v2.filestore.file_store.FileStore` subclass by dotted path,
and the selected medium is a Pydantic model so its own fields parse from the
environment under the ``MEDIUM_`` prefix (e.g. ``MEDIUM_ROOT`` for a
``LocalFileStore``). With ``MEDIUM_CLASS`` unset the config falls back to
:class:`~resourcey.v2.filestore.local_file_store.LocalFileStore`, so tests and
local development work out of the box.

The TTLs and size cap are ordinary fields under the process-wide prefix
(``APP_UPLOAD_URL_TTL_SECONDS`` / ``APP_DOWNLOAD_URL_TTL_SECONDS`` /
``APP_MAX_SIZE``). The ``*_seconds`` spelling keeps them env-parseable (the
parser has no ``timedelta``), and the ``*_ttl`` properties expose them as
``timedelta`` for call sites that prefer it.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from resourcey.v2.config.config_base import BaseConfig
from resourcey.v2.config.lazy_field import LazyField
from resourcey.v2.filestore.file_store import FileStore
from resourcey.v2.filestore.local_file_store import LocalFileStore

# The default capability TTL: short, because a signed URL is a bearer token.
DEFAULT_URL_TTL_SECONDS = 15 * 60


class FileStoreConfig(BaseConfig):
    """The file store's config block: the selected medium plus transfer limits.

    Attributes:
        medium: The selected medium instance, resolved lazily from
            ``MEDIUM_CLASS`` (default :class:`LocalFileStore`).
        upload_url_ttl_seconds: How long a ``put`` capability stays valid.
        download_url_ttl_seconds: How long a ``get`` capability stays valid.
        max_size: An optional cap on the declared upload size, in bytes.
    """

    medium: ClassVar[FileStore] = LazyField(default=LocalFileStore)  # type: ignore[assignment]
    upload_url_ttl_seconds: int = DEFAULT_URL_TTL_SECONDS
    download_url_ttl_seconds: int = DEFAULT_URL_TTL_SECONDS
    max_size: int | None = None

    @property
    def upload_url_ttl(self) -> timedelta:
        """The ``put`` capability TTL as a ``timedelta``."""
        return timedelta(seconds=self.upload_url_ttl_seconds)

    @property
    def download_url_ttl(self) -> timedelta:
        """The ``get`` capability TTL as a ``timedelta``."""
        return timedelta(seconds=self.download_url_ttl_seconds)
