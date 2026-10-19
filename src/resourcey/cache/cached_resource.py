"""``CachedResource`` — an in-memory read-through cache wrapper (issue #168).

``CachedResource`` wraps another resource and serves its reads from a
:class:`~resourcey.cache.cache_store.CacheStore` when the resource's
:class:`~resourcey.cache.cache_strategy.CacheStrategy` says the cached copy is
still fresh (:meth:`~resourcey.core.service.CacheStrategy.should_read`),
refilling the store from the source when it is not. It is the server-side
counterpart to ``filestore``'s pre-signed capabilities: a wrapper for a slow
source (e.g. a service backed by a slow HTTP entity) whose reads are otherwise
paid on every request.

Like :class:`~resourcey.view.resource_view.ResourceView` and
:class:`~resourcey.triggers.triggered_resource.TriggeredResource`, it changes
nothing observable about the resource's surface: the schema, actions, query /
sort surface, cache policy, and registration are all delegated; only the service
seam changes, wrapping the inner service in a
:class:`~resourcey.cache.cached_service.CachedService`.
``get_exposed_resource()`` returns ``self`` so the wrapper (whose service holds
the cache) is what gets registered.

The cache is **best-effort and in-memory**: it is not the source of truth, it
does not coordinate across processes, and a write through the wrapper evicts the
resource's entries. A caller-scoped (``private``) resource is never cached —
see :mod:`~resourcey.cache.cached_service`.

This module is part of ``resourcey.cache``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from pydantic import BaseModel

from resourcey.cache.cache_store import CacheStore, InMemoryCacheStore
from resourcey.cache.cache_strategy import CacheStrategy as ConcreteCacheStrategy
from resourcey.cache.cached_service import CachedService
from resourcey.core.dto import DTO, RestModels
from resourcey.core.resource import Resource
from resourcey.core.service import Action, CacheStrategy, Service, ServiceError
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


class CachedResource(Resource[T, K], Generic[T, K]):
    """A resource wrapper that caches reads in memory per the inner's strategy.

    Args:
        resource: The inner resource to wrap. Its schema, actions, query / sort
            surface, cache policy, registration, and lifecycle are all delegated.
        store: The cache medium (default a fresh
            :class:`~resourcey.cache.cache_store.InMemoryCacheStore`). Pass a
            shared / external store for a multi-worker deployment.
        private: Force the cached responses to be treated as caller-scoped, so
            **nothing** is cached (an extra guard for a resource that is
            caller-scoped without advertising it). The inner service's own
            ``response_is_private()`` already has this effect.
    """

    def __init__(
        self,
        resource: Resource[T, K],
        *,
        store: CacheStore | None = None,
        private: bool = False,
    ) -> None:
        self._inner = resource
        self._store = store if store is not None else InMemoryCacheStore()
        self._private = private
        self._entered = False

    # ------------------------------------------------------------------
    # DTO / schema surface (delegated)
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        return self._inner.get_dto_type()

    def get_rest_models(self) -> RestModels:
        return self._inner.get_rest_models()

    def get_dto_declaration(self) -> type[DTO]:
        """The inner's DTO declaration, so the manifest can validate FK references."""
        return self._inner.get_dto_declaration()  # type: ignore[attr-defined, no-any-return]  # duck-typed over the backends

    def get_id_field(self) -> str:
        return self._inner.get_id_field()

    def get_resource_path(self) -> str:
        return self._inner.get_resource_path()

    def get_cache_strategy(self) -> CacheStrategy | None:
        """The inner's cache policy (unchanged): HTTP caching is not this wrapper's job.

        The wrapper only uses the strategy *programmatically*
        (:meth:`~resourcey.core.service.CacheStrategy.should_read`); the
        response headers the transport emits are the inner's, so a caching
        wrapper does not alter what a client cache sees.
        """
        return self._inner.get_cache_strategy()

    # ------------------------------------------------------------------
    # Query / sort surface (delegated)
    # ------------------------------------------------------------------

    def get_queryable_fields(self) -> frozenset[str]:
        return self._inner.get_queryable_fields()

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        return self._inner.get_filter_operators()

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        return self._inner.get_search_filter_type()

    def get_sortable_fields(self) -> frozenset[str]:
        return self._inner.get_sortable_fields()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        return self._inner.get_sort_order_type()

    def resolve_sort_order(self, sort: str | None, desc: bool) -> Any:
        return self._inner.resolve_sort_order(sort, desc)

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        return self._inner.get_supported_actions()

    def get_exposed_resource(self) -> Resource[T, K]:
        """The wrapper is what the outside world sees (so its service caches)."""
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        """The inner service, wrapped so fresh reads are served from the cache."""
        inner = await self._inner.get_service(ctx)
        # The resource hands back the transport-facing placeholder type; the
        # concrete strategy the wrapper reads ``should_read`` off is its subtype.
        strategy = cast("ConcreteCacheStrategy[T] | None", self._inner.get_cache_strategy())
        return CachedService(
            inner,
            self._store,
            strategy,
            private=self._private,
            path=self.get_resource_path(),
        )

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        self._inner.on_register(manifest)

    def get_manifest(self) -> Manifest | None:
        return self._inner.get_manifest()

    async def __aenter__(self) -> Resource[T, K]:
        """Enter the inner resource, then the cache store."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        await self._inner.__aenter__()
        await self._store.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the cache store, then the inner resource."""
        await self._store.__aexit__(*exc)
        await self._inner.__aexit__(*exc)
        self._entered = False
