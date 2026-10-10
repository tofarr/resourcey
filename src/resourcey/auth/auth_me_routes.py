"""The ``me`` endpoint — the authenticated principal, OIDC-UserInfo shaped (issue #150).

A companion mounted **after** :func:`~resourcey.http.app.create_app` (the
``06_filestore`` / ``07_oauth`` ``register_*_routes`` pattern, since it is
genuinely not one of the eight :class:`~resourcey.core.service.Action` members),
answering "who am I?" for an authenticated caller::

    GET  .../me     the authenticated principal, OIDC UserInfo shaped
    POST .../me     the same body (OIDC UserInfo MUST support GET and POST)

It reads the **same authenticator** the app already passes to ``create_app``
(the ``AuthorizedDependencyBuilder`` / ``CompositeAuthenticator``), so the route
accepts every method the app configured — API key, session cookie, OAuth bearer
— with no extra wiring.

**Shape.** An OIDC UserInfo object (OpenID Connect Core §5.3): ``sub`` is the
REQUIRED claim and is our internal :attr:`~resourcey.auth.auth_principal.Principal.id`
(the local user id, not the provider's ``external_id`` — the local store is
authoritative and is what the ``ExternalIdentity`` map resolves to, matching the
session cookie's ``sub``). ``external_id`` / ``kind`` / ``roles`` / ``scopes``
are the framework's extension over the OIDC claim set. Profile claims (``email``,
``preferred_username``, …) are added **only** when a ``user_resource`` is
supplied and its row carries a non-``None`` value — an unavailable claim is
**omitted**, never present as ``null``.

**Not gated by the ``users`` policy.** ``me`` reads the caller's **own** row by
:attr:`~resourcey.auth.auth_principal.Principal.id` through the resource's own
service (never an :class:`~resourcey.auth.auth_authorized_service.AuthorizedService`),
so it works even where the ``users`` surface is admin-only — a ``USER`` that
cannot read ``GET /users/{id}`` can still read its own ``me``. This is not an
escalation: the route can only ever return the id the credential already proved,
and changes nothing about ``GET /users/{X}`` for any ``X``.

**Strict credential.** The route uses the strict
:func:`~resourcey.auth.auth_principal.required_principal` dependency, not the
builder's posture-gated one: ``me`` has no anonymous meaning, so an absent
credential is a ``401`` even when the app runs ``Posture.OPTIONAL``. A presented
but invalid credential is a ``401`` as always, carrying the authenticator's
challenge.

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

# The default path segment (``GET`` / ``POST /me``). OIDC calls this the UserInfo
# endpoint and conventionally serves it at ``/userinfo``; ``me`` is the
# framework's name and the OIDC shape is documented, per the resolved open
# question.
DEFAULT_ME_PATH = "me"

# The OIDC claim each profile attribute projects onto. One declaration of the
# mapping, so the served claims cannot drift from the code that reads them. A
# claim whose attribute is absent from the row (or ``None``) is omitted, per
# OIDC — an unavailable claim never appears as ``null``.
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

    Args:
        app_or_router: A ``FastAPI`` app / ``APIRouter`` (duck-typed).
        authenticator: The app's configured authenticator. The route mounts the
            strict principal dependency over it, so it accepts whichever method
            the app wired (API key / cookie / OAuth bearer) and requires a
            credential.
        user_resource: An optional **principal store** whose row enriches the
            body with profile claims (``email`` / ``preferred_username`` / …).
            The caller's own row is read by
            :attr:`~resourcey.auth.auth_principal.Principal.id`; a missing row
            simply omits the profile claims (``sub`` is still returned).
        path: The route path segment (default ``"me"``).
        prefix: An optional mount prefix.
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
    """The always-present part of the body, straight off the ``Principal``.

    ``sub`` is the internal principal id (a string), matching the session
    cookie's ``sub`` claim. ``external_id`` is included only when the principal
    authenticated through an external IdP; ``kind`` / ``roles`` / ``scopes`` are
    always present (roles / scopes possibly empty), so a client gets a
    predictable shape.
    """
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
    Only the id the credential already proved is read, so no credential can make
    this return another user's row. An unavailable attribute is omitted.
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
