"""``Manifest`` — the resource set and its lifecycle.

The Manifest owns the resources an app serves and enters/exits their runtime
lifecycle in declaration order / reverse. HTTP construction (``create_app``) is
**not** part of ``v2/core`` — it belongs to the transport layer — so the
Manifest deliberately stays a plain container.

Construction validates the action declarations of every resource up front: a
resource whose :meth:`~resourcey.v2.core.resource.Resource.get_supported_actions`
contains a non-:class:`~resourcey.v2.core.service.Action` member (a typo in a
dynamically built set) fails loudly at startup instead of silently dropping a
route.

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

from typing import Any

from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import ServiceError, assert_real_actions


class Manifest:
    """A declaration of an app's resources, owning their lifecycle.

    Attributes:
        resources: The resource instances, in declaration order.
    """

    def __init__(self, resources: tuple[Resource[Any], ...] | list[Resource[Any]]) -> None:
        self.resources: tuple[Resource[Any], ...] = tuple(resources)
        self._entered = False
        for resource in self.resources:
            assert_real_actions(type(resource).__name__, resource.get_supported_actions())

    # -- lookup ---------------------------------------------------------

    def resource_names(self) -> tuple[str, ...]:
        """The REST path segment of each resource, in declaration order."""
        return tuple(resource.get_resource_path() for resource in self.resources)

    def get_resource(self, name: str) -> Resource[Any]:
        """Return the resource whose path is ``name``; raise ``KeyError`` if absent."""
        for resource in self.resources:
            if resource.get_resource_path() == name:
                return resource
        raise KeyError(name)

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Manifest:
        """Enter each resource's runtime lifecycle in declaration order."""
        if self._entered:
            raise ServiceError("Manifest already entered")
        self._entered = True
        for resource in self.resources:
            await resource.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit each resource in reverse declaration order."""
        for resource in reversed(self.resources):
            await resource.__aexit__(*exc)
        self._entered = False

    @property
    def entered(self) -> bool:
        """Whether the manifest is currently inside its ``async with`` block."""
        return self._entered
