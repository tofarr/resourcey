"""``FileResource`` / ``FileService`` -- the ``files`` surface over a ``FileStore`` (issue #158).

Replaces the earlier ``file_metadata`` design (a SQL metadata row tracking a
synthetic ``pending`` / ``ready`` status): there is **no metadata table**
anymore. "Does the medium have the bytes" is the only source of truth for a
file's existence, so this resource is served directly over the
:class:`~resourcey.filestore.file_store.FileStore` seam -- ``read`` / ``search``
resolve against the medium's own ``head`` / ``list_objects``, uniformly across
every medium (Local / SQL / S3).

``create`` *is* the upload (case 2 of the upload-design discussion): the
transport (:mod:`resourcey.filestore.file_routes`) parses a
``multipart/form-data`` body, reads ``name`` / ``content_type`` off the upload
itself, and hands the raw bytes to :meth:`FileService.create` as the DTO's
internal-only ``content`` field -- ``size`` / ``checksum`` are computed here
from those bytes, never a client declaration. Because there is no
pre-signed-URL handshake, ``Action.CREATE`` is not in this resource's
declared action set (mirroring how ``Action.UPDATE`` was already absent): the
generic JSON create-request / batch-edit ``Create`` kind do not apply to a
multipart upload, so ``register_file_routes`` mounts the real ``POST
{resource}`` route by hand, same as it already did for ``download`` /
``content``. Authorization is unaffected -- ``AuthorizedService.create()``
checks the ``Action.CREATE`` policy regardless of what a resource declares.

Action surface: ``read``, ``delete``, ``search``, ``count``, ``batch_read``,
``batch_edit`` (narrowed by ``normalize_actions`` to the ``Delete`` kind
only -- a JSON batch body cannot carry file bytes, so batch creation is not
offered). **No ``update``**: a file's bytes are immutable once uploaded --
"changing" a file means delete the old id and create a new one.

``search`` / ``count`` have no filter or sort surface: a medium's cheap listing
call (``list_objects`` / ``count_objects``) cannot filter or sort by declared
metadata (S3's ``ListObjectsV2`` is the limiting case), so this resource does
not pretend otherwise -- only the default, keyset-by-identifier order is
offered. ``search`` also omits ``name`` / ``content_type`` / ``checksum``
(available from ``read`` only) for the same reason: ``ListObjectsV2`` cannot
report per-object user metadata.

This module imports no code outside the framework.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, MutableMapping
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from resourcey.cache.cache_defaults import DefaultCacheStrategyMixin
from resourcey.core.dto import DTO, DtoField, RestModels
from resourcey.core.errors import InvalidInputError, UnsupportedFilterError
from resourcey.core.resource import Resource
from resourcey.core.service import (
    DEFAULT_LIMIT,
    STORAGE_KEY,
    Action,
    Create,
    Delete,
    NotFoundError,
    Page,
    Service,
    ServiceError,
    Update,
)
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_store import FileStore, StoredObject
from resourcey.util.cursor import decode_cursor, encode_cursor
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")

# Fields the server owns entirely: never client input, never a factory
# default (their only source of truth is the medium). They *are* available in
# the create response -- ``create`` is the upload, so the medium has already
# reported them by the time it returns.
_SERVER_OWNED = DtoField(
    in_create_request=False,
    in_update_request=False,
    in_update_response=False,
)

# A field that exists only to carry data between the transport and the
# service, in-process, for the lifetime of one ``create`` call -- never
# projected onto any of the six REST shapes.
_INTERNAL_ONLY = DtoField(
    in_create_request=False,
    in_create_response=False,
    in_update_request=False,
    in_update_response=False,
    in_read_response=False,
    in_search_response=False,
)


class FileDTO(DTO):
    """The ``files`` DTO: a medium-native existence record, not a stored row.

    ``id`` *is* the medium's opaque storage key (a server-assigned hex string,
    not a UUID -- so the ``core`` UUID-id convention does not auto-generate it;
    :meth:`FileService.create` assigns it explicitly). ``created_at`` /
    ``updated_at`` collapse into the single ``updated_at`` (a key is written
    once and never rewritten, so there is nothing a second timestamp would
    distinguish). ``name`` / ``content_type`` / ``size`` / ``checksum`` are all
    server-derived from the upload (never client input, so none is in the
    create request) -- there is no create request left to speak of for this
    resource; see :mod:`resourcey.filestore.file_routes` for the hand-written
    multipart route. ``content`` is not a medium-native field at all: it is the
    one-request-lifetime carrier for the uploaded bytes between the transport
    and :meth:`FileService.create`, excluded from every REST shape.
    """

    id: str
    name: Annotated[str, DtoField(in_create_request=False, in_search_response=False)]
    content_type: Annotated[str | None, DtoField(in_create_request=False, in_search_response=False)]
    size: Annotated[int, DtoField(in_create_request=False)]
    checksum: Annotated[str | None, DtoField(in_create_request=False, in_search_response=False)]
    etag: Annotated[str | None, _SERVER_OWNED]
    updated_at: Annotated[datetime, _SERVER_OWNED]
    content: Annotated[bytes, _INTERNAL_ONLY]


_SUPPORTED_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.READ,
        Action.DELETE,
        Action.SEARCH,
        Action.COUNT,
        Action.BATCH_READ,
        Action.BATCH_EDIT,
    }
)


class FileResource(DefaultCacheStrategyMixin, Resource[T, K]):
    """The ``files`` resource: ``FileDTO`` served directly over a ``FileStore``.

    Args:
        store: The medium the bytes (and, for Local / SQL, the metadata) live
            in. The same instance must be entered through the manifest's
            ``managers`` slot.
        path: An explicit REST path segment (default ``"files"``).
        max_size: An optional cap on an upload's size, in bytes (default:
            :attr:`~resourcey.filestore.file_config.FileStoreConfig.max_size`).
        download_url_ttl_seconds: The download-capability TTL (default:
            :attr:`~resourcey.filestore.file_config.FileStoreConfig.download_url_ttl_seconds`).
    """

    def __init__(
        self,
        store: FileStore,
        *,
        path: str = "files",
        max_size: int | None = None,
        download_url_ttl_seconds: int | None = None,
    ) -> None:
        self._store = store
        self._path = path
        config = FileStoreConfig.get_instance()
        self._max_size = max_size if max_size is not None else config.max_size
        self._download_url_ttl_seconds = (
            download_url_ttl_seconds
            if download_url_ttl_seconds is not None
            else config.download_url_ttl_seconds
        )
        self._entered = False
        self._manifest: Manifest | None = None

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[Any]:
        """The generated DTO model (``FileDTO``'s Pydantic model)."""
        return FileDTO.get_dto_type()

    def get_dto_declaration(self) -> type[DTO]:
        """The DTO declaration this resource serves (the escape hatch)."""
        return FileDTO

    def get_rest_models(self) -> RestModels:
        return FileDTO.get_rest_models()

    def get_id_field(self) -> str:
        return FileDTO.id_field_name

    def get_resource_path(self) -> str:
        return self._path.lstrip("/")

    # get_cache_strategy is inherited from DefaultCacheStrategyMixin: ``files``
    # advertises writes (create/delete), so it is not read-only; ``updated_at``
    # is on the read model, so the default is LastModifiedCacheStrategy -- a
    # file's row never changes once it exists (there is no update action), so
    # a validator over its one write time is accurate.

    def get_queryable_fields(self) -> frozenset[str]:
        """Empty: no medium offers a cheap filter surface over its listing."""
        return frozenset()

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        return {}

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        return None

    def get_sortable_fields(self) -> frozenset[str]:
        """Empty: every medium only offers its native, identifier-ordered listing."""
        return frozenset()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        return None

    def resolve_sort_order(self, sort: str | None, desc: bool) -> SortOrder[Any] | None:
        """Always the default identifier order: ``sort`` has no surface to validate against."""
        if sort:
            raise InvalidInputError(
                f"Unknown or non-sortable sort field {sort!r}: {self.get_resource_path()} "
                "only supports the default identifier order"
            )
        return None

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        return _SUPPORTED_ACTIONS

    def get_exposed_resource(self) -> Resource[T, K] | None:
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        mapping = ctx if ctx is not None else {}
        return FileService(
            self,
            mapping,
            self._store,
            max_size=self._max_size,
            download_url_ttl_seconds=self._download_url_ttl_seconds,
        )

    @property
    def store(self) -> FileStore:
        """The medium this resource serves (the escape hatch)."""
        return self._store

    @property
    def download_url_ttl_seconds(self) -> int:
        return self._download_url_ttl_seconds

    @property
    def max_size(self) -> int | None:
        """The resolved upload-size cap, in bytes (the escape hatch for the route)."""
        return self._max_size

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        self._manifest = manifest

    def get_manifest(self) -> Manifest | None:
        return self._manifest

    async def __aenter__(self) -> Resource[T, K]:
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._entered = False


def file_resource(
    store: FileStore,
    *,
    path: str = "files",
    max_size: int | None = None,
) -> FileResource[Any, Any]:
    """Build the conventional ``files`` resource over ``store``."""
    return FileResource(store, path=path, max_size=max_size)


class FileService(Service[T, K]):
    """The ``files`` action layer: every action resolves against ``store``.

    Holds no storage of its own (the medium is the storage); entering only
    marks the lifecycle guard, mirroring :class:`~resourcey.list.list_service.ListService`.
    """

    def __init__(
        self,
        resource: FileResource[T, K],
        ctx: MutableMapping[Any, Any],
        store: FileStore,
        *,
        max_size: int | None,
        download_url_ttl_seconds: int,
    ) -> None:
        super().__init__()
        self._resource = resource
        self._ctx = ctx
        self._store = store
        self._max_size = max_size
        self._download_url_ttl_seconds = download_url_ttl_seconds

    async def __aenter__(self) -> FileService[T, K]:
        await super().__aenter__()
        self._ctx.setdefault(STORAGE_KEY, self._store)
        return self

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: Any) -> Any:
        """Commit the uploaded bytes to the medium; ``create`` *is* the upload.

        ``payload`` carries ``name`` / ``content_type`` as read off the
        upload by the transport, plus the raw bytes in ``content``. ``size`` /
        ``checksum`` are computed here, from those bytes -- never a client
        declaration to trust or verify.
        """
        self._require_entered()
        values = _values(payload)
        name = values["name"]
        content_type = values.get("content_type") or "application/octet-stream"
        data: bytes = values["content"]
        if self._max_size is not None and len(data) > self._max_size:
            raise InvalidInputError(
                f"Upload is {len(data)} bytes, exceeding the {self._max_size}-byte cap"
            )
        checksum = hashlib.sha256(data).hexdigest()
        key = uuid4().hex
        stored = await self._store.put(
            key, data, name=name, content_type=content_type, checksum=checksum
        )
        return self._full_dto(stored)

    async def read(self, id: K) -> Any:  # noqa: A002
        self._require_entered()
        found = await self._store.head(str(id))
        if found is None:
            raise NotFoundError(id)
        return self._full_dto(found)

    async def delete(self, id: K) -> None:  # noqa: A002
        self._require_entered()
        found = await self._store.head(str(id))
        if found is None:
            raise NotFoundError(id)
        await self._store.delete(str(id))

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        self._require_entered()
        self._reject_filter(search_filter)
        after = self._after_key(cursor)
        found = await self._store.list_objects(after=after, limit=limit + 1)
        has_more = len(found) > limit
        page = found[:limit]
        items = [self._search_dto(obj) for obj in page]
        next_cursor = self._encode_cursor(page[-1].key) if has_more and page else None
        return Page(items=items, limit=limit, next_cursor=next_cursor)

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        self._require_entered()
        self._reject_filter(search_filter)
        return await self._store.count_objects()

    async def batch_read(self, ids: list[K]) -> list[Any]:
        self._require_entered()
        results = []
        for id_value in ids:
            found = await self._store.head(str(id_value))
            results.append(self._full_dto(found) if found is not None else None)
        return results

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[Any]:
        self._require_entered()
        supported = self._resource.get_supported_actions()
        results: list[Any] = []
        for edit in edits:
            if isinstance(edit, Create):
                raise InvalidInputError(
                    "batch_edit cannot create a file: uploading requires multipart/form-data, "
                    "which a JSON batch body cannot carry. POST to the resource directly."
                )
            elif isinstance(edit, Update):
                raise InvalidInputError("batch_edit cannot update: files have no update action")
            else:
                if Action.DELETE not in supported:
                    raise InvalidInputError("batch_edit cannot delete: delete is not supported")
                found = await self._store.head(str(edit.id))
                if found is not None:
                    await self._store.delete(str(edit.id))
                results.append(None)
        return results

    # ------------------------------------------------------------------
    # DTO projection
    # ------------------------------------------------------------------

    def _dto_type(self) -> type[Any]:
        return self._resource.get_dto_type()

    def _full_dto(self, obj: StoredObject) -> Any:
        """Every field (the ``read`` / ``batch_read`` shape)."""
        return self._dto_type()(
            id=obj.key,
            name=obj.name,
            content_type=obj.content_type,
            size=obj.size,
            checksum=obj.checksum,
            etag=obj.etag,
            updated_at=obj.updated_at,
        )

    def _search_dto(self, obj: StoredObject) -> Any:
        """Only the medium-listing fields (``name`` / ``content_type`` /
        ``checksum`` stay ``MISSING``, dropped by the transport's projection
        onto ``search_response``, which does not declare them)."""
        return self._dto_type()(
            id=obj.key,
            size=obj.size,
            etag=obj.etag,
            updated_at=obj.updated_at,
        )

    # ------------------------------------------------------------------
    # Filtering / paging
    # ------------------------------------------------------------------

    def _reject_filter(self, search_filter: SearchFilter[Any] | None) -> None:
        """Defense in depth for a direct service caller (the transport already
        rejects any ``field__op`` param -- the query surface is empty)."""
        if search_filter is not None:
            raise UnsupportedFilterError(
                f"{type(self._resource).__name__} has no filter surface: a medium's listing "
                "call cannot filter by declared metadata"
            )

    def _after_key(self, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        try:
            sort_field, ascending, _sort_key, id_value = decode_cursor(self._encryption(), cursor)
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc
        if sort_field is not None or not ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; start a "
                "new search without a cursor."
            )
        return str(id_value)

    def _encode_cursor(self, key: str) -> str:
        return encode_cursor(
            self._encryption(), sort_field=None, ascending=True, sort_key=key, id_value=key
        )

    def _encryption(self) -> EncryptionService:
        return get_encryption_service()


def _values(payload: Any) -> dict[str, Any]:
    """The explicitly supplied (non-``MISSING``) fields of a DTO instance."""
    return {name: value for name, value in vars(payload).items() if value is not MISSING}
