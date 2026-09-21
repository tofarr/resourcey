"""Security wiring for example 03.

:class:`SecuredSqlResource` is the ``SqlResource`` subclass every resource in
this example derives from. It overrides ``open_service`` to wrap the bare
:class:`~resourcey.resource.service.SqlService` in a
:class:`~resourcey.auth.secured_service.SecuredService`, keyed by the resource
class name, with a :class:`~resourcey.auth.permission_resolver.PermissionResolver`
backed by the request's DB session.

The principal (``user_id``) is resolved from the request's
``Authorization: Bearer`` header or session cookie via
:class:`~resourcey.auth.auth_tokens.TokenService`; a missing token means
anonymous access (``user_id=None``), and a present-but-invalid token is a 401.

Default permissions are **denied** (fail-closed): the ``DefaultPermissions``
passed to the resolver is empty, so the only access comes from explicit
``UserPermission`` rows. This matches the ``PermissionResolver`` default
behaviour (no matching policy → ``None`` → deny).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from resourcey.auth.auth_models import AuthToken
from resourcey.auth.auth_tokens import InvalidTokenError, TokenService
from resourcey.auth.permission_resolver import DefaultPermissions, PermissionResolver
from resourcey.auth.secured_service import SecuredService
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.service import SqlService
from resourcey.resource.sql import SqlResource

# Shared fail-closed defaults: no policy applies to any principal unless an
# explicit ``UserPermission`` row grants it.
_DEFAULT_PERMISSIONS = DefaultPermissions()


class SecuredSqlResource(SqlResource):
    """A ``SqlResource`` whose ``open_service`` yields a ``SecuredService``."""

    def open_service(self, request: Any) -> Any:
        """Yield a ``SecuredService`` wrapping a ``SqlService`` for ``request``."""
        return _open_secured_service(self, request)


async def _resolve_principal(request: Any, session: Any) -> AuthToken | None:
    """Resolve the authenticated principal's token, or ``None`` (anonymous).

    Reads the ``Authorization: Bearer`` header or the session cookie, then
    authenticates it via :class:`TokenService`. A missing token means anonymous
    (``None``); a present-but-invalid token is a 401, so a secured resource
    never silently treats a bad credential as anonymous.
    """
    bearer = request.headers.get("authorization", "")
    token_value: str | None
    if bearer.lower().startswith("bearer "):
        token_value = bearer[7:].strip()
    else:
        cfg = get_config_as(FrameworkConfig)
        token_value = request.cookies.get(cfg.auth.cookie_name)
    if not token_value:
        return None
    try:
        return await TokenService(session).authenticate(token_value)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired auth token.",
        ) from exc


def _token_user_id(token: AuthToken | None) -> UUID | None:
    return token.user_id if token is not None else None


@asynccontextmanager
async def _open_secured_service(resource: SecuredSqlResource, request: Any) -> Any:
    """Open (or reuse) a session and yield a ``SecuredService`` for ``request``.

    Mirrors ``resourcey.resource.sql._open_sql_service``: the session on
    ``request.state.session`` is reused across resources in one request so all
    resources share a single transaction; the caller that opened it owns the
    commit/close. Otherwise a new session is opened from the resource's
    session factory, committed on success, and rolled back on error.

    The inner ``SqlService`` is wrapped in a ``SecuredService`` whose
    ``PermissionResolver`` is bound to the same session and the shared
    fail-closed defaults. The principal is resolved once per request (cached
    on ``request.state``) so multiple secured resources share the resolution.
    """
    token = getattr(request.state, "_secured_principal", _NOT_RESOLVED)

    session = getattr(request.state, "session", None)
    if session is not None:
        if token is _NOT_RESOLVED:
            token = await _resolve_principal(request, session)
            object.__setattr__(request.state, "_secured_principal", token)
        yield _build_secured(resource, session, token)
        return

    factory = resource._session_factory
    if factory is None:
        raise ResourceyConfigError(
            f"{type(resource).__name__} has no session factory — its lifespan "
            "was not entered (no manifest / app_context)."
        )
    async with factory() as session:
        request.state.session = session
        try:
            if token is _NOT_RESOLVED:
                token = await _resolve_principal(request, session)
                object.__setattr__(request.state, "_secured_principal", token)
            yield _build_secured(resource, session, token)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _build_secured(
    resource: SecuredSqlResource,
    session: Any,
    token: AuthToken | None,
) -> SecuredService:
    """Wrap a ``SqlService`` in a ``SecuredService`` for the principal."""
    inner = SqlService(resource, session=session)
    resolver = PermissionResolver(session, defaults=_DEFAULT_PERMISSIONS)
    user_id = _token_user_id(token)
    return SecuredService(
        inner=inner,
        resource_type=type(resource).__name__,
        resource_name=type(resource).__name__,
        user_id=user_id,
        groups=frozenset(),
        resolver=resolver.resolve,
    )


_NOT_RESOLVED: Any = object()
