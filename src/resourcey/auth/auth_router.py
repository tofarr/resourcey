"""HTTP routes for the federated OAuth (auth) feature (issue #4).

Ported from ohev2's ``auth_router.py``, adapted to resourcey.

Endpoints:

* ``GET  /auth/authorize`` — validate client + redirect URI, redirect to IdP.
* ``GET  /auth/callback``  — exchange IdP code, set session cookie, redirect.
* ``POST /auth/token``    — exchange auth code for access + refresh tokens.
* ``POST /auth/refresh``  — rotate the access + refresh pair via the IdP.
* ``POST /auth/revoke``   — revoke a token (RFC 7009).
* ``POST /auth/logout``   — cookie-focused session end.
* ``GET  /auth/userinfo`` — OIDC UserInfo claims for the authenticated principal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response

from resourcey.auth.auth_dependencies import depends_access_token
from resourcey.auth.auth_models import AuthToken
from resourcey.auth.auth_schemas import TokenRequest, TokenResponse, UserInfoResponse
from resourcey.auth.auth_service import (
    AuthError,
    AuthService,
    IdpError,
    InvalidClientError,
    InvalidGrantError,
    InvalidRedirectUriError,
    RefreshLockTimeoutError,
    TokenPair,
    _mint_cookie_jwe,
    _seconds_until,
)
from resourcey.auth.session import SessionDep
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.encryption.encryption_service import get_encryption_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _callback_url() -> str:
    base_url = get_config_as(FrameworkConfig).base_url.rstrip("/")
    return f"{base_url}/auth/callback"


def _set_session_cookie(
    response: RedirectResponse,
    *,
    user_id: uuid.UUID,
    access_id: uuid.UUID,
    access_expires_at: datetime,
) -> None:
    cfg = get_config_as(FrameworkConfig)
    enc = get_encryption_service()
    cookie_token = _mint_cookie_jwe(
        enc,
        user_id=user_id,
        access_id=access_id,
        access_expires_at=access_expires_at,
    )
    response.set_cookie(
        key=cfg.auth.cookie_name,
        value=cookie_token,
        max_age=max(1, int((access_expires_at - datetime.now(UTC)).total_seconds())),
        httponly=True,
        samesite=cfg.auth.cookie_samesite,
        secure=cfg.auth.cookie_secure,
        path="/",
    )


def _error_status(exc: AuthError) -> int:
    if isinstance(exc, InvalidClientError):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, InvalidRedirectUriError):
        return status.HTTP_400_BAD_REQUEST
    if isinstance(exc, InvalidGrantError):
        return status.HTTP_400_BAD_REQUEST
    if isinstance(exc, RefreshLockTimeoutError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, IdpError):
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_400_BAD_REQUEST


def _to_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type="Bearer",
        expires_in=pair.expires_in,
        expires_at=pair.access_expires_at,
        refresh_token_expires_in=_seconds_until(pair.refresh_expires_at),
        refresh_token_expires_at=pair.refresh_expires_at,
        id_token=pair.id_token,
    )


@router.get("/authorize")
async def authorize(
    session: SessionDep,
    response_type: Annotated[Literal["code", "cookie"], Query()],
    client_id: Annotated[str, Query()],
    redirect_uri: Annotated[str, Query()],
    state: Annotated[str | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
    code_challenge: Annotated[str | None, Query()] = None,
    code_challenge_method: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Validate the client + redirect URI and redirect to the IdP."""
    service = AuthService(session)
    try:
        url = await service.build_authorize_redirect(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            callback_url=_callback_url(),
            response_type=response_type,
        )
    except AuthError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    finally:
        await service.aclose()
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    session: SessionDep,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
) -> RedirectResponse:
    """Exchange the IdP code and redirect to the client."""
    service = AuthService(session)
    try:
        ctx = await service.handle_callback(
            code=code,
            state=state,
            callback_url=_callback_url(),
        )
    except AuthError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    finally:
        await service.aclose()
    await session.commit()

    params: dict[str, str] = {}
    if ctx.auth_code is not None:
        params["code"] = ctx.auth_code
    if ctx.client_state is not None:
        params["state"] = ctx.client_state
    location = f"{ctx.redirect_uri}?{urlencode(params)}" if params else ctx.redirect_uri
    response = RedirectResponse(url=location, status_code=status.HTTP_302_FOUND)
    if ctx.response_type == "cookie":
        _set_session_cookie(
            response,
            user_id=ctx.user_id,
            access_id=ctx.access_id,
            access_expires_at=ctx.access_expires_at,
        )
    return response


@router.post("/token", response_model=TokenResponse)
async def token(
    payload: TokenRequest,
    session: SessionDep,
) -> TokenResponse:
    """Exchange an authorization code (or refresh token) for our tokens."""
    service = AuthService(session)
    try:
        if payload.grant_type == "authorization_code":
            if payload.code is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="code is required for authorization_code grant.",
                )
            pair = await service.exchange_authorization_code(
                code=payload.code,
                redirect_uri=payload.redirect_uri or "",
                client_id=payload.client_id,
                client_secret=payload.client_secret,
                code_verifier=payload.code_verifier,
            )
        elif payload.grant_type == "refresh_token":
            if payload.refresh_token is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="refresh_token is required for refresh_token grant.",
                )
            pair = await service.exchange_refresh_token(
                refresh_token=payload.refresh_token,
                client_id=payload.client_id,
                client_secret=payload.client_secret,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="grant_type must be 'authorization_code' or 'refresh_token'.",
            )
    except AuthError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    finally:
        await service.aclose()
    await session.commit()
    return _to_response(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: TokenRequest,
    session: SessionDep,
) -> TokenResponse:
    """Rotate the access + refresh pair via the IdP (refresh grant)."""
    if payload.grant_type != "refresh_token":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="grant_type must be 'refresh_token'.",
        )
    if payload.refresh_token is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="refresh_token is required.",
        )
    service = AuthService(session)
    try:
        pair = await service.exchange_refresh_token(
            refresh_token=payload.refresh_token,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
        )
    except AuthError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    finally:
        await service.aclose()
    await session.commit()
    return _to_response(pair)


@router.post("/revoke", status_code=status.HTTP_200_OK)
async def revoke(
    token: Annotated[str, Form()],
    session: SessionDep,
    token_type_hint: Annotated[str | None, Form()] = None,
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
) -> Response:
    """Revoke a token (RFC 7009). Best-effort; always returns 200."""
    service = AuthService(session)
    try:
        await service.revoke_token(
            token=token,
            token_type_hint=token_type_hint,
            client_id=client_id,
            client_secret=client_secret,
        )
    except InvalidClientError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except AuthError:
        pass
    finally:
        await service.aclose()
    await session.commit()
    return Response(status_code=status.HTTP_200_OK)


@router.get("/userinfo", response_model=UserInfoResponse)
async def userinfo(
    session: SessionDep,
    token: Annotated[AuthToken, Depends(depends_access_token)],
) -> UserInfoResponse:
    """Return OIDC UserInfo claims for the authenticated principal."""
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An access token is required.",
        )
    service = AuthService(session)
    try:
        claims = await service.build_userinfo_claims(token.user_id, token.scopes)
    finally:
        await service.aclose()
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled.",
        )
    return UserInfoResponse(
        sub=claims.get("sub", str(token.user_id)),
        email=claims.get("email"),
        email_verified=claims.get("email_verified"),
        name=claims.get("name"),
        preferred_username=claims.get("preferred_username"),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
) -> Response:
    """End the browser session. Clears the cookie; best-effort federated revoke."""
    cfg = get_config_as(FrameworkConfig)
    cookie_token = request.cookies.get(cfg.auth.cookie_name)
    if cookie_token is not None:
        service = AuthService(session)
        try:
            await service.revoke_session(cookie_token)
        except AuthError:
            pass
        finally:
            await service.aclose()
        await session.commit()
    response.delete_cookie(
        key=cfg.auth.cookie_name,
        httponly=True,
        samesite=cfg.auth.cookie_samesite,
        secure=cfg.auth.cookie_secure,
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# ---------------------------------------------------------------------- #
# OIDC Discovery.
# ---------------------------------------------------------------------- #


@router.get("/.well-known/openid-configuration")
async def openid_configuration(session: SessionDep) -> dict[str, Any]:
    """OIDC discovery document (OIDC Discovery §3)."""
    service = AuthService(session)
    try:
        return service.build_discovery_document()
    finally:
        await service.aclose()


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(session: SessionDep) -> dict[str, Any]:
    """RFC 8414 OAuth 2.0 authorization-server metadata."""
    service = AuthService(session)
    try:
        return service.build_discovery_document()
    finally:
        await service.aclose()
