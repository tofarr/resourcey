"""The per-request service-dependency seam (issue #86).

A :class:`DependencyBuilder` decides **how** the per-request service a route
handler receives is built. It is passed to the transport entry points, resolved
once per resource at route-registration time, and used to build that resource's
FastAPI dependency — so one setting can apply an authorization posture to the
whole API without naming a resource.

:class:`OpenDependencyBuilder` is the default and secures nothing, so it
appears deliberately in an app's assembly rather than being reached by
forgetting to pass a securing builder. Because
:meth:`DependencyBuilder.get_service_dependency` returns an ordinary
**callable**, a builder's author may declare any parameter FastAPI can wire
(the request, other ``Depends(...)``, i.e. an auth dependency), which is what
makes this seam cover authentication as well as authorization.

The builder is an **authorization** seam: it never decides *exposure*
(``get_exposed_resource`` is the sole gate on route registration) and returns a
non-optional callable, so it cannot silently drop routes. A restrictive posture
should return a dependency that *denies* rather than nothing, keeping the route
visible in OpenAPI.

The builder lives in ``http`` rather than on the manifest because the default
produces a FastAPI dependency annotated with ``starlette.requests.Request``,
and ``core`` must not import ``http`` (a cycle).

This module imports no code outside the framework.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, MutableMapping
from typing import Any, TypeVar, cast

from fastapi import Request

from resourcey.core.resource import Resource
from resourcey.core.service import Service
from resourcey.util.models import DiscriminatedUnionMixin

T = TypeVar("T")

# The key under which a request's call-scoped ctx lives on ``request.state``.
_CTX_STATE_KEY = "resourcey_ctx"


class DependencyBuilder(DiscriminatedUnionMixin, ABC):
    """Build the FastAPI dependency that yields a resource's per-request service."""

    @abstractmethod
    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., object]:
        """Generate a service dependency for the resource given.

        The returned callable is used directly as a FastAPI dependency, so it
        may declare any parameter FastAPI can wire.
        """

    def get_principal_dependency(self) -> Callable[..., object] | None:
        """An optional dependency the transport adds to every route's ``dependencies``.

        An authenticating builder returns one here so its security scheme appears
        in the OpenAPI operation. It guards the route without becoming a declared
        parameter, and the authenticated result is cached per request, so there is
        no double authentication.
        """
        return None


class OpenDependencyBuilder(DependencyBuilder):
    """The no-auth default: the resource's own service over the request-scoped ctx.

    Named ``Open`` because it secures nothing — a deployment must supply an
    authenticating builder instead.
    """

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., object]:
        async def dependency(request: Request) -> AsyncIterator[Service[Any, Any]]:
            service = await resource.get_service(request_ctx(request))
            async with service:
                yield service

        return dependency


def request_ctx(request: Request) -> MutableMapping[Any, Any]:
    """The call-scoped context for ``request`` (created on first use).

    Every resource in one request shares this mapping, so storage opened by one
    service is adopted by the next.
    """
    ctx = getattr(request.state, _CTX_STATE_KEY, None)
    if ctx is None:
        ctx = {}
        setattr(request.state, _CTX_STATE_KEY, ctx)
    return cast("MutableMapping[Any, Any]", ctx)
