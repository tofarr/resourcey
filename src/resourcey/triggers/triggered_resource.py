"""``TriggeredResource`` — attach edit-event triggers to an existing resource (issue #155).

``TriggeredResource`` wraps another resource and, after every successful
create / update / delete / batch_edit, invokes each registered
:class:`~resourcey.triggers.trigger.Trigger`. The primary use case is
**webhooks**: notify an external system when rows change.

Unlike :class:`~resourcey.view.resource_view.ResourceView`, a trigger changes
nothing observable about the resource — no field is hidden, no action is
narrowed, the cache policy is unaffected. Every surface-defining method is
therefore a plain delegation to the inner resource; only the service seam
changes, wrapping the inner service in a
:class:`~resourcey.triggers.triggered_service.TriggeredService`.

``get_exposed_resource()`` returns ``self`` (not the inner's exposed resource)
for the same reason :class:`~resourcey.view.resource_view.ResourceView` does:
the wrapper is what must be registered so its (triggering) service is what the
transport mounts — registering the inner instead would reach a service with no
triggers attached.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from resourcey.core.dto import RestModels
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import Action, CacheStrategy, Service, ServiceError
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.triggered_service import TriggeredService
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

if TYPE_CHECKING:
    from resourcey.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


def _validate_on_edit(triggers: Sequence[Any]) -> None:
    """Reject a bare callable or a duplicate trigger, at construction time.

    ``on_edit`` accepts only :class:`Trigger` instances — a bare callable
    would lose the config fields ``Trigger`` exists to carry — and rejects a
    duplicate (by value equality) so the same side effect cannot silently
    fire twice per operation.
    """
    for trigger in triggers:
        if not isinstance(trigger, Trigger):
            raise ResourceyConfigError(
                f"on_edit accepts only Trigger instances; got {type(trigger).__name__!r} "
                "(a bare callable would lose the config fields Trigger exists to carry)."
            )
    seen: list[Trigger] = []
    for trigger in triggers:
        if any(trigger == existing for existing in seen):
            raise ResourceyConfigError(f"Duplicate trigger in on_edit: {trigger!r}")
        seen.append(trigger)


class TriggeredResource(Resource[T, K], Generic[T, K]):
    """A resource wrapper that fires ``on_edit`` triggers after a successful edit.

    Args:
        resource: The inner resource to wrap. Its schema, actions, query /
            sort surface, cache policy, registration, and lifecycle are all
            delegated verbatim — a trigger changes nothing observable.
        on_edit: The triggers to invoke after a successful create / update /
            delete / batch_edit, in declaration order. Must be ``Trigger``
            instances with no duplicates (validated here).
        background: Whether firing happens in the background (the default) or
            is awaited inline before the write returns. See
            :class:`~resourcey.triggers.triggered_service.TriggeredService`.
    """

    def __init__(
        self,
        resource: Resource[T, K],
        *,
        on_edit: Sequence[Trigger] = (),
        background: bool = True,
    ) -> None:
        _validate_on_edit(on_edit)
        self._inner = resource
        self._triggers = list(on_edit)
        self._background = background
        self._entered = False

    # ------------------------------------------------------------------
    # DTO / schema surface (delegated)
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        return self._inner.get_dto_type()

    def get_rest_models(self) -> RestModels:
        return self._inner.get_rest_models()

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
        """The inner service, wrapped so every configured trigger fires on success."""
        inner = await self._inner.get_service(ctx)
        return TriggeredService(inner, self._triggers, background=self._background)

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
        await self._inner.__aexit__(*exc)
        self._entered = False
