"""Security wiring for example 03.

:class:`SecuredDependencyBuilder` is the deployment's ``DependencyBuilder``
(issue #62): it composes, for every resource at once, a per-request dependency
that wraps the bare :class:`~resourcey.resource.service.SqlService` in a
:class:`~resourcey.auth.secured_service.SecuredService`, keyed by the exposed
resource's class name and backed by a
:class:`~resourcey.auth.permission_resolver.PermissionResolver` on the request's
DB session.

This is the seam the issue intended for auth wiring: the resource declares what
exists, the builder decides how each request is served. The builder is selected
by ``DEPENDENCY_BUILDER_CLASS`` in ``.env`` (the ``LazyField`` convention), so
no resource in the example needs to know about RBAC.

The principal (``user_id``) is resolved from the request's
``Authorization: Bearer`` header or session cookie via
:class:`~resourcey.auth.auth_tokens.TokenService`; a missing token means
anonymous access (``user_id=None``), and a present-but-invalid token is a 401.
It is resolved once per request (cached on ``request.state``) so multiple
secured resources share the resolution.

Default permissions are **denied** (fail-closed): the ``DefaultPermissions``
passed to the resolver is empty, so the only access comes from explicit
``UserPermission`` rows. This matches the ``PermissionResolver`` default
behaviour (no matching policy -> ``None`` -> deny).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request, status
from resourcey.auth.auth_models import AuthToken
from resourcey.auth.auth_tokens import InvalidTokenError, TokenService
from resourcey.auth.permission_resolver import DefaultPermissions, PermissionResolver
from resourcey.auth.secured_service import SecuredService
from resourcey.config.config_dependency import DependencyBuilder
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.resource.base import BaseResource
from resourcey.resource.service import SqlService

# Shared fail-closed defaults: no policy applies to any principal unless an
# explicit ``UserPermission`` row grants it.
_DEFAULT_PERMISSIONS = DefaultPermissions()


class SecuredDependencyBuilder(DependencyBuilder):
    """Compose a ``SecuredService`` dependency per resource (issue #62)."""

    def get_service_dependency(self, resource: BaseResource) -> Callable[..., Any]:
        """Return an async-generator dependency bound to ``resource``.

        The returned callable is the FastAPI dependency the route builder uses;
        it opens the resource's per-request storage via ``open_storage`` (so all
        secured resources in one request share a session / transaction), then
        yields a ``SecuredService``. Auth stays a ``Depends`` target, so FastAPI
        still surfaces the security scheme in the OpenAPI schema (issue #62).
        """

        async def dependency(request: Request) -> AsyncIterator[Any]:
            async with resource.open_storage(request) as session:
                token = await _resolve_principal(request, session)
                yield _build_secured(resource, session, token)

        return dependency


async def _resolve_principal(request: Request, session: Any) -> AuthToken | None:
    """Resolve the authenticated principal's token, or ``None`` (anonymous).

    Reads the ``Authorization: Bearer`` header or the session cookie, then
    authenticates it via :class:`TokenService`. Cached per request so every
    secured resource in one request shares the resolution. A missing token
    means anonymous (``None``); a present-but-invalid token is a 401, so a
    secured resource never silently treats a bad credential as anonymous.
    """
    cached = getattr(request.state, "_secured_principal", _NOT_RESOLVED)
    if cached is not _NOT_RESOLVED:
        return cast(AuthToken | None, cached)

    bearer = request.headers.get("authorization", "")
    token_value: str | None
    if bearer.lower().startswith("bearer "):
        token_value = bearer[7:].strip()
    else:
        cfg = get_config_as(FrameworkConfig)
        token_value = request.cookies.get(cfg.auth.cookie_name)

    token: AuthToken | None
    if not token_value:
        token = None
    else:
        try:
            token = await TokenService(session).authenticate(token_value)
        except InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired auth token.",
            ) from exc
    object.__setattr__(request.state, "_secured_principal", token)
    return token


def _token_user_id(token: AuthToken | None) -> UUID | None:
    return token.user_id if token is not None else None


def _build_secured(resource: BaseResource, session: Any, token: AuthToken | None) -> SecuredService:
    """Wrap a ``SqlService`` (bound to ``resource``) in a ``SecuredService``."""
    inner = SqlService(resource, session=session)
    resolver = PermissionResolver(session, defaults=_DEFAULT_PERMISSIONS)
    return SecuredService(
        inner=inner,
        resource_type=type(resource).__name__,
        resource_name=type(resource).__name__,
        user_id=_token_user_id(token),
        groups=frozenset(),
        resolver=resolver.resolve,
        actions=resource.actions,
    )


_NOT_RESOLVED: Any = object()
