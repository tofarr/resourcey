"""``Manifest`` — the resource set and its lifecycle.

The Manifest owns the resources an app serves and enters/exits their runtime
lifecycle in declaration order / reverse. HTTP construction (``create_app``) is
**not** part of ``core`` — it belongs to the transport layer — so the
Manifest deliberately stays a plain container.

Construction validates the action declarations of every resource up front: a
resource whose :meth:`~resourcey.core.resource.Resource.get_supported_actions`
contains a non-:class:`~resourcey.core.service.Action` member (a typo in a
dynamically built set) fails loudly at startup instead of silently dropping a
route. It then calls :meth:`~resourcey.core.resource.Resource.on_register`
(sync) on each resource, handing it the manifest so it can resolve sibling
resources *lazily, later* (e.g. to verify foreign keys when a request needs it)
— never from within the hook itself, since registration ordering is not a
contract.

This module is part of the ``core`` bottom layer: it imports no other
``resourcey`` module.

Refer to the ``DtoField.references`` docstring for the fail-fast reference check.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any

from resourcey.core.errors import ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import ServiceError, assert_real_actions
from resourcey.util.naming import singularise


def _validate_references(resources: tuple[Resource[Any, Any], ...]) -> None:
    """Fail loudly when a DTO field's ``references`` names an unserved resource.



    A DTO field may declare ``DtoField.references`` — the **singular resource name**
    the field points at (e.g. ``"thread"``, never the plural REST path
    ``"threads"``). Runs when the manifest starts (see :meth:`Manifest.__aenter__`)
    so an unknown target — a resource forgotten from the manifest, or a wrong
    derived singularisation — fails loudly instead of surfacing as a confusing
    runtime error when the FK is enforced. Resources without a DTO declaration
    (e.g. a hand-written ``Resource``) are simply skipped: there is nothing
    to validate. An unset or ``None`` reference carries no constraint.
    """
    served = {singularise(resource.get_resource_path()) for resource in resources}
    for resource in resources:
        getter = getattr(resource, "get_dto_declaration", None)
        if getter is None:
            continue
        for name, config in getter().get_fields().items():
            target = config.references
            if not isinstance(target, str):
                continue
            if target not in served:
                raise ResourceyConfigError(
                    f"{resource.get_resource_path()}: DTO field {name!r} references "
                    f"{target!r}, but no resource in the manifest serves the resource "
                    f"name {target!r} (served: {sorted(served)}); declare the referenced "
                    "resource or set the DtoField references explicitly."
                )


class Manifest:
    """A declaration of an app's resources, owning their lifecycle.

    Attributes:
        resources: The resource instances, in declaration order.
        managers: App-lifecycle async context managers (e.g. a session manager)
            entered before the resources and exited after them. Deliberately
            generic, not typed to any backend: ``core`` must not import
            ``sql``, and later resources / config can hang their own
            lifecycle off the same slot.
    """

    def __init__(
        self,
        resources: tuple[Resource[Any, Any], ...] | list[Resource[Any, Any]],
        managers: tuple[AbstractAsyncContextManager[Any], ...]
        | list[AbstractAsyncContextManager[Any]] = (),
    ) -> None:
        self.resources: tuple[Resource[Any, Any], ...] = tuple(resources)
        self.managers: tuple[AbstractAsyncContextManager[Any], ...] = tuple(managers)
        self._entered = False
        for resource in self.resources:
            assert_real_actions(type(resource).__name__, resource.get_supported_actions())
        for resource in self.resources:
            resource.on_register(self)

    # -- lookup ---------------------------------------------------------

    def resource_names(self) -> tuple[str, ...]:
        """The REST path segment of each resource, in declaration order."""
        return tuple(resource.get_resource_path() for resource in self.resources)

    def get_resource(self, name: str) -> Resource[Any, Any]:
        """Return the resource whose path is ``name``; raise ``KeyError`` if absent."""
        for resource in self.resources:
            if resource.get_resource_path() == name:
                return resource
        raise KeyError(name)

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Manifest:
        """Enter managers, then each resource's lifecycle in declaration order."""
        if self._entered:
            raise ServiceError("Manifest already entered")
        self._entered = True
        _validate_references(self.resources)

        for manager in self.managers:
            await manager.__aenter__()
        for resource in self.resources:
            await resource.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit resources in reverse order, then managers (reverse of entry)."""
        for resource in reversed(self.resources):
            await resource.__aexit__(exc_type, exc, tb)
        for manager in reversed(self.managers):
            await manager.__aexit__(exc_type, exc, tb)
        self._entered = False

    @property
    def entered(self) -> bool:
        """Whether the manifest is currently inside its ``async with`` block."""
        return self._entered
