"""The per-request service-dependency seam (issue #62).

A :class:`DependencyBuilder` decides **how** the per-request service a route
handler receives is built. It is resolved once per resource at route
registration time from :attr:`~resourcey.config.config_framework.FrameworkConfig.dependency_builder`,
so one setting swaps the posture for every resource at once.

* :class:`DefaultDependencyBuilder` — the default. Returns the resource's own
  :meth:`~resourcey.resource.base.BaseResource.get_service_dependency`, i.e.
  today's behaviour (a storage-backed service bound to a session).
* An app-supplied builder — e.g. one that wraps every resource's service in
  :class:`~resourcey.auth.secured_service.SecuredService` and composes an auth
  dependency, so authorization is applied globally without per-resource code.

The builder is an **authorization** seam. It never decides *exposure*:
:meth:`~resourcey.resource.base.BaseResource.get_exposed_resource` is the sole
gate on route registration, and :meth:`DependencyBuilder.get_service_dependency`
returns a **non-optional** callable, so a builder cannot silently drop routes.
A restrictive posture should return a dependency that *denies* (403) rather
than nothing, keeping the route (and the denial) visible in OpenAPI.

The module deliberately does not import ``resource.base`` at runtime (only under
``TYPE_CHECKING``): the default builder merely returns an attribute, and keeping
the import out lets ``config_framework`` import this module without a cycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from resourcey.util.models import DiscriminatedUnionMixin

if TYPE_CHECKING:
    from resourcey.resource.base import BaseResource


class DependencyBuilder(DiscriminatedUnionMixin, ABC):
    """Build the FastAPI dependency that yields a resource's per-request service."""

    @abstractmethod
    def get_service_dependency(self, resource: BaseResource) -> Callable[..., object]:
        """Generate a service dependency for the resource given."""


class DefaultDependencyBuilder(DependencyBuilder):
    """The default builder: use the resource's own service dependency.

    Preserves pre-#62 behaviour exactly — the resource decides how to open a
    service for the request, and the builder adds nothing.
    """

    def get_service_dependency(self, resource: BaseResource) -> Callable[..., object]:
        return resource.get_service_dependency
