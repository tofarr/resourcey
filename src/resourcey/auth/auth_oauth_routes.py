"""The interactive OAuth flow routes — login / callback / refresh (issue #151).

A companion mounted **after** :func:`~resourcey.http.app.create_app`, reading the
same client resource — the ``06_filestore`` pattern (``register_file_routes`` +
a metadata resource). It owns the interactive flow, which is genuinely not one of
the eight :class:`~resourcey.core.service.Action` members::

    GET  .../oauth/login      redirect to the provider's auth_url (state + PKCE)
    GET  .../oauth/callback   exchange the code, persist the token, mint our session
    POST .../oauth/refresh    the token service's locked refresh

**Session posture: BFF.** The callback mints **our own** session cookie
(:class:`~resourcey.auth.auth_cookie.CookieAuthenticator` /
``EncryptionService.create_jwe_token``); the browser presents *our* cookie, never
the provider's access token. The provider's refresh token lives only, encrypted,
in the token table; the cookie carries an opaque handle (the framework JWE with a
``sub``) the server maps back to the stored token. Encoding a provider credential
into a cookie or an API key is explicitly out.

**Expiry alignment.** Our session token's ``exp`` is the **same expiry as the
external IdP token** — the provider is authoritative for session lifetime — and
the browser cookie's ``Max-Age`` is clamped down to it, never longer than the
provider grants.

**Ephemeral flow state.** ``state``, the PKCE verifier, and the authorization
code are seconds-lived and single-use, so they are carried in a short-TTL JWE
cookie (``EncryptionService``), not a table.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from resourcey.auth.auth_config import SessionCookieConfig
from resourcey.auth.auth_oauth_config import IdpConfig
from resourcey.auth.auth_oauth_provider import (
    OAuthCredentialProvider,
    TokenResponse,
    authorize_url,
)
from resourcey.auth.auth_oauth_token import OAuthTokenService, StoredToken
from resourcey.auth.auth_principal import Principal, PrincipalKind
from resourcey.auth.auth_role import roles_from_credential
from resourcey.core.errors import InvalidInputError
from resourcey.core.resource import Resource
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service

# The flow-state JWE claim keys (carried in the short-TTL state cookie).
_STATE_CLAIM = "state"
_VERIFIER_CLAIM = "verifier"
_CLIENT_CLAIM = "client"
_REDIRECT_CLAIM = "redirect"

# The session cookie's JWE claim keys (``sub`` mirrors CookieAuthenticator).
_SUB_CLAIM = "sub"
_ROLES_CLAIM = "roles"
_EXTERNAL_CLAIM = "ext"

# The callback route's ``name`` (so ``request.url_for`` can build its URL).
_CALLBACK_NAME = "oauth_callback"


def register_oauth_routes(
    app_or_router: FastAPI | APIRouter,
    client_resource: Resource[Any, Any],
    *,
    token_resource: Resource[Any, Any] | None = None,
    identity_resource: Resource[Any, Any] | None = None,
    config: IdpConfig | None = None,
    session_config: SessionCookieConfig | None = None,
    encryption_service: EncryptionService | None = None,
    http_post: Callable[..., Any] | None = None,
    prefix: str = "",
) -> APIRouter:
    """Mount the interactive login / callback / refresh routes.

    Args:
        app_or_router: A ``FastAPI`` app / ``APIRouter`` (duck-typed).
        client_resource: The **inner** client resource (config-list or DB-backed).
        token_resource: The token resource; required for the callback / refresh
            (the login redirect needs only the client resource).
        identity_resource: The ``ExternalIdentity`` resource; required to resolve
            ``(iss, sub)`` to an internal user on callback.
        config: The OAuth config block (flow paths / state TTL). Default
            :meth:`IdpConfig.get_instance`.
        session_config: The session-cookie config. Default
            :meth:`SessionCookieConfig.get_instance`.
        encryption_service: The service that mints the flow-state / session JWEs.
            Defaults to the process-wide one.
        http_post: An injectable token-exchange poster (tests inject a fake).
        prefix: An optional mount prefix.
    """
    resolved = config if config is not None else IdpConfig.get_instance()
    session = session_config if session_config is not None else SessionCookieConfig.get_instance()
    encryption = encryption_service or get_encryption_service()

    router = APIRouter(tags=["OAuth"])

    _add_login_route(router, client_resource, resolved, session, encryption)
    if token_resource is not None and identity_resource is not None:
        _add_callback_route(
            router,
            client_resource,
            token_resource,
            identity_resource,
            resolved,
            session,
            encryption,
            http_post,
        )
        _add_refresh_route(
            router,
            client_resource,
            token_resource,
            resolved,
            session,
            encryption,
            http_post,
        )

    app_or_router.include_router(router, prefix="" if prefix == "/" else prefix)
    return router


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _add_login_route(
    router: APIRouter,
    client_resource: Resource[Any, Any],
    config: IdpConfig,
    session: SessionCookieConfig,
    encryption: EncryptionService,
) -> None:
    async def login(client: str, request: Request) -> Any:
        record = await _read_client(client_resource, client)
        redirect_uri = _redirect_uri(record, request)
        verifier = _code_verifier()
        state = secrets.token_urlsafe(32)
        url = authorize_url(
            record, state=state, redirect_uri=redirect_uri, code_challenge=_code_challenge(verifier)
        )
        state_token = encryption.create_jwe_token(
            {
                _STATE_CLAIM: state,
                _VERIFIER_CLAIM: verifier,
                _CLIENT_CLAIM: client,
                _REDIRECT_CLAIM: redirect_uri,
            },
            expires_in=timedelta(seconds=config.oauth_flow_state_ttl_seconds),
        )
        response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        _set_flow_cookie(response, config, session, state_token)
        return response

    login.__name__ = "oauth_login"
    _route(
        router,
        "/" + config.oauth_login_path.lstrip("/"),
        ["GET"],
        login,
        summary="Begin the OAuth login flow",
        description="Redirect the browser to the provider's authorization endpoint (state + PKCE).",
    )


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


def _add_callback_route(
    router: APIRouter,
    client_resource: Resource[Any, Any],
    token_resource: Resource[Any, Any],
    identity_resource: Resource[Any, Any],
    config: IdpConfig,
    session: SessionCookieConfig,
    encryption: EncryptionService,
    http_post: Callable[..., Any] | None,
) -> None:
    async def callback(request: Request, code: str | None = None, state: str | None = None) -> Any:
        flow = _read_flow_state(request, config, encryption)
        if flow is None:
            raise InvalidInputError("Missing or expired OAuth flow state")
        if not state or state != flow.get(_STATE_CLAIM):
            raise InvalidInputError("OAuth state mismatch")
        if not code:
            raise InvalidInputError("Missing authorization code")
        record = await _read_client(client_resource, str(flow.get(_CLIENT_CLAIM)))
        verifier = flow.get(_VERIFIER_CLAIM)

        token_service = await _token_service(token_resource)
        async with token_service:
            provider = OAuthCredentialProvider(token_service, http_post)
            exchanged = await provider.exchange_code(
                record,
                code=code,
                redirect_uri=str(flow.get(_REDIRECT_CLAIM)),
                code_verifier=str(verifier) if verifier else None,
            )
            principal = await _resolve_principal(identity_resource, record, exchanged)
            stored = await token_service.store(
                principal_id=cast(UUID, principal.id),
                client_id=record.id,
                access_token=exchanged.access_token,
                refresh_token=exchanged.refresh_token,
                expires_at=exchanged.expires_at,
                refresh_expires_at=exchanged.refresh_expires_at,
                scope=exchanged.scope,
            )
        response = _session_response(principal, record, stored, session, encryption)
        # The flow state is single-use: clear the state cookie on consumption so
        # a replay within its TTL cannot re-exchange the same code.
        response.delete_cookie(
            config.oauth_state_cookie_name,
            path="/",
            secure=session.session_cookie_secure,
        )
        return response

    callback.__name__ = _CALLBACK_NAME
    _route(
        router,
        "/" + config.oauth_callback_path.lstrip("/"),
        ["GET"],
        callback,
        summary="Complete the OAuth login flow",
        description="Exchange the code, persist the token, and mint our session cookie.",
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _add_refresh_route(
    router: APIRouter,
    client_resource: Resource[Any, Any],
    token_resource: Resource[Any, Any],
    config: IdpConfig,
    session: SessionCookieConfig,
    encryption: EncryptionService,
    http_post: Callable[..., Any] | None,
) -> None:
    async def refresh(request: Request, client: str) -> Any:
        principal = _principal_from_session(request, session, encryption)
        if principal is None or principal.id is None:
            raise InvalidInputError("No active session to refresh")
        record = await _read_client(client_resource, client)
        token_service = await _token_service(token_resource)
        async with token_service:
            provider = OAuthCredentialProvider(token_service, http_post)
            token = await token_service.refresh(
                principal_id=principal.id, client=record, provider=provider
            )
        return _session_response(principal, record, token, session, encryption)

    refresh.__name__ = "oauth_refresh"
    _route(
        router,
        "/" + config.oauth_refresh_path.lstrip("/"),
        ["POST"],
        refresh,
        summary="Refresh the session",
        description="Exchange the stored refresh token for a fresh session (locked refresh).",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_client(client_resource: Resource[Any, Any], client_id: str) -> Any:
    service = await client_resource.get_service({})
    async with service:
        return await service.read(client_id)


async def _token_service(token_resource: Resource[Any, Any]) -> OAuthTokenService:
    """Open the token resource's service, typed as :class:`OAuthTokenService`."""
    service = await token_resource.get_service({})
    return cast(OAuthTokenService, service)


async def _resolve_principal(
    identity_resource: Resource[Any, Any], record: Any, token: TokenResponse
) -> Principal:
    """Resolve the internal principal for the exchanged token's ``(iss, sub)``.

    The token is trusted here (the code was just exchanged over TLS with the
    provider); inbound verification is
    :class:`~resourcey.auth.auth_oauth.OAuthAuthenticator`'s job for machine
    clients presenting a token directly. ``(iss, sub)`` is resolved through the
    ``ExternalIdentity`` mapping; a first-seen pair is **fail-closed**.
    """
    claims = _decode_unverified(token.access_token)
    subject = claims.get("sub")
    issuer = claims.get("iss") or getattr(record, "issuer", None)
    if not isinstance(subject, str) or not subject:
        raise InvalidInputError("Provider token carries no subject")
    service: Any = await identity_resource.get_service({})
    async with service:
        mapping = await service.find_by_issuer_subject(str(issuer), subject)
    if mapping is None:
        raise InvalidInputError("No local user is linked to this external identity; fail-closed.")
    return Principal(
        id=_as_uuid(mapping.user_id),
        kind=PrincipalKind.USER,
        external_id=subject,
        roles=roles_from_credential(getattr(record, "roles", None)),
    )


def _session_response(
    principal: Principal,
    record: Any,
    token: StoredToken,
    session: SessionCookieConfig,
    encryption: EncryptionService,
) -> Any:
    """Mint our session cookie, its ``exp`` matching the IdP token's expiry.

    The browser cookie's ``Max-Age`` is clamped down to the IdP expiry, so our
    session never outlives the provider's.
    """
    remaining = max(int((token.expires_at - datetime.now(UTC)).total_seconds()), 0)
    max_age = min(session.session_cookie_ttl_seconds, remaining)
    claims: dict[str, Any] = {
        _SUB_CLAIM: str(principal.id),
        _ROLES_CLAIM: sorted(principal.roles),
    }
    if principal.external_id is not None:
        claims[_EXTERNAL_CLAIM] = principal.external_id
    jwe = encryption.create_jwe_token(claims, expires_in=timedelta(seconds=remaining))
    response = JSONResponse(content={"status": "ok"})
    response.set_cookie(
        session.session_cookie_name,
        jwe,
        max_age=max_age,
        httponly=True,
        secure=session.session_cookie_secure,
        samesite=session.session_cookie_samesite,
        domain=session.session_cookie_domain,
        path=session.session_cookie_path,
    )
    return response


def _set_flow_cookie(
    response: Any, config: IdpConfig, session: SessionCookieConfig, token: str
) -> None:
    response.set_cookie(
        config.oauth_state_cookie_name,
        token,
        max_age=config.oauth_flow_state_ttl_seconds,
        httponly=True,
        secure=session.session_cookie_secure,
        samesite="lax",
    )


def _read_flow_state(
    request: Request, config: IdpConfig, encryption: EncryptionService
) -> dict[str, Any] | None:
    raw = request.cookies.get(config.oauth_state_cookie_name)
    if not raw:
        return None
    try:
        claims = encryption.decrypt_jwe_token(raw)
    except (ValueError, KeyError, TypeError):
        return None
    return claims if isinstance(claims, dict) else None


def _principal_from_session(
    request: Request, session: SessionCookieConfig, encryption: EncryptionService
) -> Principal | None:
    raw = request.cookies.get(session.session_cookie_name)
    if not raw:
        return None
    try:
        claims = encryption.decrypt_jwe_token(raw)
    except (ValueError, KeyError, TypeError):
        return None
    try:
        user_id = _as_uuid(claims.get(_SUB_CLAIM))
    except (ValueError, TypeError):
        return None
    return Principal(
        id=user_id,
        kind=PrincipalKind.USER,
        external_id=claims.get(_EXTERNAL_CLAIM),
        roles=roles_from_credential(claims.get(_ROLES_CLAIM)),
    )


def _redirect_uri(record: Any, request: Request) -> str:
    configured = getattr(record, "redirect_uri", None)
    if configured:
        return str(configured)
    return str(request.url_for(_CALLBACK_NAME))


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode_unverified(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _route(
    router: APIRouter,
    path: str,
    methods: list[str],
    handler: Callable[..., Any],
    *,
    summary: str,
    description: str | None = None,
) -> None:
    """Add a route unless one already exists at that path + method (escape hatch)."""
    existing = {
        (getattr(route, "path", None), m)
        for route in router.routes
        for m in getattr(route, "methods", set())
    }
    for method in methods:
        if (path, method) in existing:
            continue
        router.add_api_route(
            path,
            handler,
            methods=[method],
            summary=summary,
            description=description,
            status_code=status.HTTP_200_OK,
            name=handler.__name__,
        )
