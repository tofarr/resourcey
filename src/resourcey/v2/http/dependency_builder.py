"""The per-request service-dependency seam for ``v2`` (issue #86).

A :class:`DependencyBuilder` decides **how** the per-request service a route
handler receives is built. It is passed to the transport entry points
(:func:`~resourcey.v2.http.app.create_app` /
:func:`~resourcey.v2.http.app.add_to_app`), resolved once per resource at
route-registration time, and used to build that resource's FastAPI dependency —
so one setting can apply an authorization posture (e.g. wrap every service in a
secured service) to the whole API without naming a resource.

* :class:`DefaultDependencyBuilder` — the default. It produces a dependency
  that opens the resource's own service over the request-scoped ``ctx`` and
  yields it, i.e. today's behaviour with no wrapper.
* An app-supplied builder — e.g. one that wraps every resource's service and
  composes an auth dependency. Because
  :meth:`DependencyBuilder.get_service_dependency` returns an ordinary
  **callable**, its author may declare any parameter FastAPI can wire (the
  :class:`~starlette.requests.Request`, other ``Depends(...)``, i.e. an auth
  dependency). That is how the ``v1`` builder composed an API-key check with
  the service, and it is what makes this seam cover authentication as well as
  authorization.

The builder is an **authorization** seam. It never decides *exposure*:
:meth:`~resourcey.v2.core.resource.Resource.get_exposed_resource` is the sole
gate on route registration, and
:meth:`DependencyBuilder.get_service_dependency` returns a **non-optional**
callable, so a builder cannot silently drop routes. A restrictive posture
should return a dependency that *denies* (403) rather than nothing, keeping the
route (and the denial) visible in OpenAPI.

The builder and its default live here, in ``v2/http``, rather than on the
:class:`~resourcey.v2.core.manifest.Manifest`: the default must produce a
FastAPI dependency whose ``Request`` parameter is annotated
``starlette.requests.Request`` (FastAPI locates the request by that
annotation), so the builder is transport code, and ``v2/core`` must not import
``v2/http`` (a cycle). The manifest therefore stays a plain container, and the
seam threads through the transport entry points.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, MutableMapping
from typing import Any, TypeVar, cast

from fastapi import Request

from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Service
from resourcey.v2.util.models import DiscriminatedUnionMixin

T = TypeVar("T")

# The key under which a request's call-scoped ctx lives on ``request.state``.
_CTX_STATE_KEY = "resourcey_ctx"


class DependencyBuilder(DiscriminatedUnionMixin, ABC):
    """Build the FastAPI dependency that yields a resource's per-request service."""

    @abstractmethod
    def get_service_dependency(self, resource: Resource[Any]) -> Callable[..., object]:
        """Generate a service dependency for the resource given.

        The returned callable is used directly as a FastAPI dependency, so it
        may declare any parameter FastAPI can wire.
        """


class DefaultDependencyBuilder(DependencyBuilder):
    """The default builder: the resource's own service over the request-scoped ctx."""

    def get_service_dependency(self, resource: Resource[Any]) -> Callable[..., object]:
        async def dependency(request: Request) -> AsyncIterator[Service[Any]]:
            async with resource.get_service(request_ctx(request)) as service:
                yield service

        return dependency


def request_ctx(request: Request) -> MutableMapping[Any, Any]:
    """The call-scoped context for ``request`` (created on first use).

    Every resource in one request shares this mapping, so storage opened by one
    service is adopted by the next (see
    :mod:`resourcey.v2.core.resource` on storage ownership).
    """
    ctx = getattr(request.state, _CTX_STATE_KEY, None)
    if ctx is None:
        ctx = {}
        setattr(request.state, _CTX_STATE_KEY, ctx)
    return cast("MutableMapping[Any, Any]", ctx)
