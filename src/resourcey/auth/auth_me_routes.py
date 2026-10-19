"""The ``me`` endpoint — the authenticated principal, OIDC-UserInfo shaped (issue #150).

A companion mounted **after** ``create_app`` (the ``06_filestore`` / ``07_oauth``
``register_*_routes`` pattern), answering "who am I?" for an authenticated
caller::

    GET  .../me     the authenticated principal, OIDC UserInfo shaped
    POST .../me     the same body (OIDC UserInfo MUST support GET and POST)

It reads the **same authenticator** the app already passes to ``create_app``, so
the route accepts every method the app configured with no extra wiring.

**Shape.** An OIDC UserInfo object: ``sub`` is REQUIRED and is our internal
``Principal.id`` (the local user id, not the provider's ``external_id``).
``external_id`` / ``kind`` / ``roles`` / ``scopes`` are the framework's extension
over the OIDC claim set. Profile claims are added **only** when a
``user_resource`` is supplied and its row carries a non-``None`` value — an
unavailable claim is **omitted**, never present as ``null``.

**Not gated by the ``users`` policy.** ``me`` reads the caller's **own** row
through the resource's own service (never an ``AuthorizedService``), so it works
even where the ``users`` surface is admin-only. This is not an escalation: the
route can only ever return the id the credential already proved.

**Strict credential.** The route uses ``required_principal``, not the builder's
posture-gated dependency: ``me`` has no anonymous meaning.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, status

from resourcey.auth.auth_principal import (
    Authenticator,
    Principal,
    required_principal,
)
from resourcey.core.resource import Resource
from resourcey.core.service import NotFoundError
from resourcey.util.missing import MISSING

# The default path segment (``GET`` / ``POST /me``).
DEFAULT_ME_PATH = "me"

# A claim whose attribute is absent from the row (or ``None``) is omitted.
PROFILE_CLAIM_MAP: dict[str, str] = {
    "email": "email",
    "email_verified": "email_verified",
    "preferred_username": "username",
    "name": "name",
    "updated_at": "updated_at",
}


def register_me_routes(
    app_or_router: FastAPI | APIRouter,
    *,
    authenticator: Authenticator,
    user_resource: Resource[Any, Any] | None = None,
    path: str = DEFAULT_ME_PATH,
    prefix: str = "",
) -> APIRouter:
    """Mount the ``me`` route (``GET`` / ``POST``) on ``app_or_router``.

    ``user_resource`` is an optional principal store whose row enriches the body
    with profile claims; a missing row simply omits them.
    """
    dependency = required_principal(authenticator)
    router = APIRouter(tags=["Auth"])

    async def me(principal: Principal = Depends(dependency)) -> dict[str, Any]:  # noqa: B008
        body = _principal_body(principal)
        body.update(await _profile_claims(user_resource, principal))
        return body

    me.__name__ = "me"
    for method in ("GET", "POST"):
        _route(router, "/" + path.strip("/"), method, me)

    app_or_router.include_router(router, prefix="" if prefix == "/" else prefix)
    return router


def _principal_body(principal: Principal) -> dict[str, Any]:
    """The always-present part of the body, straight off the ``Principal``."""
    body: dict[str, Any] = {
        "sub": str(principal.id),
        "kind": principal.kind.value,
        "roles": sorted(principal.roles),
        "scopes": sorted(principal.scopes),
    }
    if principal.external_id is not None:
        body["external_id"] = principal.external_id
    return body


async def _profile_claims(
    user_resource: Resource[Any, Any] | None, principal: Principal
) -> dict[str, Any]:
    """The caller's profile claims, read from its **own** row.

    The lookup opens the resource's own service directly (never an
    ``AuthorizedService``), so the ``users`` read grant does not gate ``me``.
    """
    if user_resource is None or principal.id is None:
        return {}
    service = await user_resource.get_service({})
    async with service:
        try:
            row = await service.read(principal.id)
        except NotFoundError:
            return {}
    claims: dict[str, Any] = {}
    for claim, attribute in PROFILE_CLAIM_MAP.items():
        value = getattr(row, attribute, MISSING)
        if value is MISSING or value is None:
            continue
        claims[claim] = value
    return claims


def _route(router: APIRouter, path: str, method: str, handler: Callable[..., Any]) -> None:
    """Add a route unless one already exists at that path + method (escape hatch)."""
    existing = {
        (getattr(route, "path", None), m)
        for route in router.routes
        for m in getattr(route, "methods", set())
    }
    if (path, method) in existing:
        return
    router.add_api_route(
        path,
        handler,
        methods=[method],
        summary="The authenticated principal (OIDC UserInfo shape)",
        description=(
            "Return the caller's own principal as an OIDC-UserInfo-shaped object. "
            "A credential is required; an absent or invalid one is 401."
        ),
        status_code=status.HTTP_200_OK,
        name=handler.__name__,
    )
