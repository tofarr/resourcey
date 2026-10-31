"""``TriggeredResource`` — attach edit-event triggers to an existing resource (issue #155).

``TriggeredResource`` wraps another resource and, after every successful write,
invokes each registered ``Trigger``. The primary use case is **webhooks**.

Unlike ``ResourceView``, a trigger changes nothing observable about the resource,
so every surface-defining method is a plain delegation; only the service seam
changes, wrapping the inner service in a ``TriggeredService``.
``get_exposed_resource()`` returns ``self`` so the wrapper is what gets
registered — registering the inner would reach a service with no triggers.

At construction, every trigger implementing the optional ``ResourceBoundTrigger``
protocol has ``bind_resource`` called with the **inner** resource, so a trigger
that projects onto the read model sees exactly the narrowed surface.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from resourcey.core.dto import DTO, RestModels
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import Action, CacheStrategy, Service, ServiceError, StorageKey
from resourcey.triggers.trigger import ResourceBoundTrigger, Trigger
from resourcey.triggers.trigger_runner import TriggerRunner
from resourcey.triggers.triggered_service import TriggeredService
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


def _validate_on_edit(triggers: Sequence[Any]) -> None:
    """Reject a bare callable or a duplicate trigger, at construction time."""
    for trigger in triggers:
        if not isinstance(trigger, Trigger):
            raise ResourceyConfigError(
                f"on_edit accepts only Trigger instances; got {type(trigger).__name__!r} "
                "(a bare callable would lose the config fields Trigger exists to carry)."
            )
    seen: list[Trigger[Any, Any]] = []
    for trigger in triggers:
        if any(trigger == existing for existing in seen):
            raise ResourceyConfigError(f"Duplicate trigger in on_edit: {trigger!r}")
        seen.append(trigger)


class TriggeredResource(Resource[T, K], Generic[T, K]):
    """A resource wrapper that fires ``on_edit`` triggers after a successful edit.

    ``on_edit`` must be ``Trigger`` instances with no duplicates (validated
    here). ``background`` selects background firing (the default) or awaiting
    inline.
    """

    def __init__(
        self,
        resource: Resource[T, K],
        *,
        on_edit: Sequence[Trigger[T, K]] = (),
        background: bool = True,
    ) -> None:
        _validate_on_edit(on_edit)
        self._inner = resource
        self._triggers = list(on_edit)
        self._background = background
        for trigger in self._triggers:
            if isinstance(trigger, ResourceBoundTrigger):
                trigger.bind_resource(resource)
        # Shared across every per-request TriggeredService; only closed by this
        # resource's own __aexit__, never by a per-request service's exit.
        self._runner = TriggerRunner()
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
        """The wrapper is what the outside world sees (so its service fires triggers)."""
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        """The inner service, wrapped so every configured trigger fires on success.

        Every call shares this resource's one ``TriggerRunner``, so a background
        run outlives the per-request service that started it.
        """
        inner = await self._inner.get_service(ctx)
        return TriggeredService(
            inner, self._triggers, background=self._background, runner=self._runner
        )

    async def get_storage_key(self) -> StorageKey:
        """The inner resource's storage key: a wrapper adds no storage of its own."""
        return await self._inner.get_storage_key()

    # ------------------------------------------------------------------
    # Registration / lifecycle (delegated to the inner resource)
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        self._inner.on_register(manifest)

    def get_manifest(self) -> Manifest | None:
        return self._inner.get_manifest()

    async def __aenter__(self) -> Resource[T, K]:
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Drain this resource's shared ``TriggerRunner``, then exit the inner.

        This is the app-scoped exit a background trigger run is cancelled at.
        """
        await self._runner.aclose()
        await self._inner.__aexit__(*exc)
        self._entered = False
