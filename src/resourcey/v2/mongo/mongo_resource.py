"""``MongoResource`` — the ``v2`` Mongo backend (issue #80).

Mongo has no schema of record, so — unlike the model-first SQL path — the
developer **declares the DTO** and the resource serves it::

    class Thread(DTO):
        id: UUID
        title: str

    manifest = Manifest(resources=[MongoResource(Thread, name="main")])

The DTO declaration drives everything: the six REST models, the identifier, the
query surface (the read model *is* the filter / sort surface — the same security
gate ``SqlResource`` uses), and the resource path (the DTO name, pluralized).
A bare ``id: UUID`` gets a server-side ``uuid4`` create default from the
``v2/core`` conventions, so ``v1``'s ``_make_id_optional`` hack is unnecessary,
and ``created_at`` / ``updated_at`` are framework-owned.

The session source mirrors ``SqlResource``'s escape hatches: an explicit
``client=`` (or a client pre-seeded on ``ctx``) wins; otherwise the collection is
resolved from ``client_manager`` by ``name``, defaulting to the process-wide
:func:`~resourcey.v2.mongo.mongo_client.get_mongo_client_manager` and its first
connection. Resolving a connection is async, so :meth:`get_service` is async
(same as SQL).

Indexes are opt-in and app-driven (:meth:`get_indexes` / :meth:`ensure_indexes`,
run from :meth:`__aenter__`) since there is no DDL / migration step, and
:meth:`migrate_document` is the manual migration-on-read hook (default no-op)
invoked on every read path by the service.

The action layer lives in :mod:`resourcey.v2.mongo.mongo_service`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel

from resourcey.v2.cache.cache_defaults import DefaultCacheStrategyMixin
from resourcey.v2.core.dto import DTO, RestModels
from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import STORAGE_KEY, Action, Service, ServiceError
from resourcey.v2.encryption.encryption_service import get_encryption_service
from resourcey.v2.mongo.mongo_client import (
    DEFAULT_MONGO_DATABASE,
    MongoClientManager,
    get_mongo_client_manager,
)
from resourcey.v2.mongo.mongo_filter_converter import MongoFilterContext, MongoFilterConverter
from resourcey.v2.mongo.mongo_service import MongoService
from resourcey.v2.mongo.mongo_sort_converter import MongoSortContext, MongoSortConverter
from resourcey.v2.util.naming import camel_to_kebab, camel_to_snake, pluralize
from resourcey.v2.util.search_filter import SearchFilter, operators_for_annotation
from resourcey.v2.util.sort_order import AttrSortOrder, SortOrder

if TYPE_CHECKING:
    from resourcey.v2.core.manifest import Manifest
    from resourcey.v2.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")

# The Mongo primary-key field: the DTO identifier is stored under it.
MONGO_ID_FIELD = "_id"


class MongoResource(DefaultCacheStrategyMixin, Resource[T, K]):
    """The Mongo backend: a DTO declaration served by :class:`MongoService`.

    Args:
        dto: The :class:`~resourcey.v2.core.dto.DTO` declaration to serve. Its
            fields become the DTO / REST models and the query surface.
        client_manager: The manager the collection's client is resolved from
            (default: the process-wide :func:`get_mongo_client_manager`). It must
            be entered (via the manifest) before the first service is built.
        name: The connection name to resolve from ``client_manager`` (default:
            the first configured connection).
        path: An explicit REST path segment (defaults to the DTO name, pluralized).
        encryption_service: The service used to encrypt pagination cursors.
            Defaults to the process-wide
            :func:`~resourcey.v2.encryption.encryption_service.get_encryption_service`;
            pass one to override.
        client: An explicit client (the escape hatch) — wins over ``client_manager``.
        database_name: The database to select on ``client`` (default: the
            resolved connection's database, or ``DEFAULT_MONGO_DATABASE``).
        collection_name: An explicit collection name (defaults to the DTO name,
            pluralized / snake-cased).
    """

    def __init__(
        self,
        dto: type[DTO],
        *,
        client_manager: MongoClientManager | None = None,
        name: str | None = None,
        path: str | None = None,
        encryption_service: EncryptionService | None = None,
        client: Any = None,
        database_name: str | None = None,
        collection_name: str | None = None,
    ) -> None:
        self._dto = dto
        self._path = path
        self._client_manager = client_manager
        self._connection_name = name
        self._client = client
        self._database_name = database_name
        self._collection_name = collection_name
        # Resolved eagerly so cursor pagination works out of the box; the
        # explicit argument remains the escape hatch.
        self._encryption_service = encryption_service or get_encryption_service()
        self._collection: Any = None
        self._entered = False
        self._manifest: Manifest | None = None

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The DTO model (the declaration's generated Pydantic model)."""
        return cast("type[T]", self._dto.get_dto_type())

    def get_dto_declaration(self) -> type[DTO]:
        """The DTO declaration class this resource serves (the escape hatch)."""
        return self._dto

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the DTO declaration's ``in_*`` flags."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name (``id`` unless the DTO declares another)."""
        return self._dto.id_field_name

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the DTO name, pluralized."""
        if self._path is not None:
            return self._path.lstrip("/")
        return pluralize(camel_to_kebab(self._dto.__name__)).lower()

    # get_cache_strategy is inherited from DefaultCacheStrategyMixin: read-only
    # → optimistic, else last-modified when ``updated_at`` is readable, else ETag.

    def get_queryable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default query surface.

        Derived from ``read_response``, so a field projected away is not
        filterable: a wrapper that hides ``secret`` must not leave
        ``?secret__eq=`` disclosing it.
        """
        return frozenset(self.get_rest_models().read_response.model_fields)

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        """The derived filter surface: each queryable field's allowed operators."""
        fields = self.get_rest_models().read_response.model_fields
        return {name: operators_for_annotation(field.annotation) for name, field in fields.items()}

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        """A declared object-filter class, or ``None`` (derive from the read model)."""
        return None

    def get_sortable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default sort surface."""
        return self.get_queryable_fields()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        """A declared :class:`SortOrder` class, or ``None`` (derive the surface)."""
        return None

    def resolve_sort_order(self, sort: str | None, desc: bool) -> SortOrder[Any] | None:
        """Validate ``sort`` / ``desc`` into the ordering a search will use.

        With no ``sort`` the identifier order is used and ``desc`` is ignored.
        An unknown or non-sortable field raises
        :class:`~resourcey.v2.core.errors.InvalidInputError`.
        """
        if not sort:
            return None
        declared = self.get_sort_order_type()
        if isinstance(declared, type) and issubclass(declared, AttrSortOrder):
            if sort not in self.get_sortable_fields():
                raise InvalidInputError(f"Unknown or non-sortable sort field {sort!r}")
            return declared(attribute=sort, descending=desc)
        if sort not in self.get_sortable_fields():
            raise InvalidInputError(f"Unknown or non-sortable sort field {sort!r}")
        return AttrSortOrder(attribute=sort, descending=desc)

    def mongo_field_for(self, attribute: str) -> str:
        """The Mongo document field backing DTO attribute ``attribute``.

        The identifier maps to Mongo's ``_id``; every other field keeps its name.
        """
        if attribute == self.get_id_field():
            return MONGO_ID_FIELD
        return attribute

    # Opt-in escape hatch: when True, an unconvertible filter falls back to an
    # in-memory scan instead of raising. Off by default.
    allow_filter_iteration: bool = False

    # ------------------------------------------------------------------
    # Filter conversion seam
    # ------------------------------------------------------------------

    def build_filter_context(self) -> MongoFilterContext:
        """The conversion context: only *queryable* fields resolve to Mongo fields.

        Restricting ``fields`` to :meth:`get_queryable_fields` is the security
        gate: a field projected away from the read model has no field here, so
        ``?secret__eq=`` raises rather than disclosing the value.
        """
        queryable = self.get_queryable_fields()
        fields = {attr: self.mongo_field_for(attr) for attr in queryable}
        return MongoFilterContext(fields=fields, id_field=self.get_id_field())

    def build_filter_converter(self) -> MongoFilterConverter:
        """A converter over this resource's query surface."""
        return MongoFilterConverter(self.build_filter_context())

    # ------------------------------------------------------------------
    # Sort conversion seam
    # ------------------------------------------------------------------

    def build_sort_context(self) -> MongoSortContext:
        """The sort context: only *sortable* fields resolve to Mongo fields.

        Restricting ``fields`` to :meth:`get_sortable_fields` is the security
        gate, mirroring filtering: a field projected away from the read model
        has no field here, so ``?sort=secret`` raises rather than leaking the
        hidden value's relative order.
        """
        sortable = self.get_sortable_fields()
        fields = {attr: self.mongo_field_for(attr) for attr in sortable}
        return MongoSortContext(fields=fields, id_field=self.get_id_field())

    def build_sort_converter(self) -> MongoSortConverter:
        """A converter over this resource's sort surface."""
        return MongoSortConverter(self.build_sort_context())

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        """Every :class:`Action`; a subclass narrows by overriding."""
        return frozenset(Action)

    def get_exposed_resource(self) -> Resource[T, K] | None:
        """The resource the outside world sees (default: ``self``)."""
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        """Build a :class:`MongoService` over ``ctx`` and a collection.

        A collection already seeded on ``ctx`` (under ``STORAGE_KEY``) wins — the
        same reuse seam the SQL service uses; an explicit ``client`` wins over
        the manager; otherwise the client is resolved from ``client_manager`` —
        the injected one, else the process-wide :func:`get_mongo_client_manager`
        — by ``connection_name``. Resolving by name makes this method async.
        """
        mapping = ctx if ctx is not None else {}
        collection = mapping.get(STORAGE_KEY)
        if collection is None:
            collection = await self._resolve_collection()
        return MongoService(self, mapping, collection)

    async def _resolve_collection(self) -> Any:
        """The collection this resource serves, resolved lazily and cached.

        An explicit ``client`` wins; otherwise the client is resolved from the
        manager by connection name. The result is cached on the instance (the
        manager caches the underlying client) so repeated services share it.
        """
        if self._collection is not None:
            return self._collection
        client, database_name = self._client, self._database_name
        if client is None:
            manager = self._client_manager or get_mongo_client_manager()
            client, resolved_name = await manager.get_client(self._connection_name)
            database_name = database_name or resolved_name
        name = self._collection_name or self.get_collection_name()
        self._collection = client[database_name or DEFAULT_MONGO_DATABASE][name]
        return self._collection

    def get_collection_name(self) -> str:
        """The collection name: the DTO name, snake-cased, pluralized, lowercased."""
        return pluralize(camel_to_snake(self._dto.__name__)).lower()

    @property
    def collection(self) -> Any:
        """The resolved collection, or ``None`` before the first service is built.

        The escape hatch back to ``motor``: a caller may drop to the raw
        collection for anything the standard actions do not cover.
        """
        return self._collection

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        """Record the manifest that owns this resource."""
        self._manifest = manifest

    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this resource, or ``None``."""
        return self._manifest

    async def __aenter__(self) -> Resource[T, K]:
        """Enter the lifecycle: resolve the collection, then run :meth:`ensure_indexes`."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        await self._resolve_collection()
        await self.ensure_indexes()
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the lifecycle, dropping the resolved collection.

        The manager closes its clients on exit, so the cached collection would
        reference a closed client on the next entry; clearing it forces
        :meth:`_resolve_collection` to re-resolve from the rebuilt client. (A
        collection seeded on ``ctx`` is unaffected — it is never cached here.)
        """
        self._collection = None
        self._entered = False

    # ------------------------------------------------------------------
    # Indexes (opt-in, application-driven — no DDL / migration step)
    # ------------------------------------------------------------------

    def get_indexes(self) -> list[dict[str, Any]]:
        """Index specifications for :meth:`ensure_indexes`.

        Each spec is ``{"key": [(field, direction)], "name": str | None,
        "options": dict}``. The default is empty (only the ``_id`` index exists);
        override to declare secondary indexes.
        """
        return []

    async def ensure_indexes(self) -> None:
        """Create the indexes declared by :meth:`get_indexes` on the collection."""
        indexes = self.get_indexes()
        if not indexes:
            return
        collection = self._collection
        for spec in indexes:
            await collection.create_index(
                spec["key"], name=spec.get("name"), **spec.get("options", {})
            )

    # ------------------------------------------------------------------
    # Manual migration on read (opt-in, application-defined)
    # ------------------------------------------------------------------

    def migrate_document(self, document: dict[str, Any]) -> dict[str, Any]:
        """Upgrade a stored document to the current shape before it is read.

        Default no-op. Override to lazily migrate a document on every read path
        (read / search / batch-read / batch-edit results); the versioning scheme
        is application-defined.
        """
        return document
