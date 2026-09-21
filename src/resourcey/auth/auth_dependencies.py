"""FastAPI dependencies for auth + authorization resolution (issue #4).

Ported from ohev2's ``auth_dependencies.py``, adapted to resourcey's
session management and config. This module resolves the authenticated
principal from a federated credential and caches the per-request work so
subsequent dependencies in the same request reuse it.

Credential resolution priority:

1. The ``Authorization: Bearer <token>`` header — an auth access-token JWE.
2. The session cookie (``ttyp: cookie``) set by the auth callback.

A token that is missing entirely means anonymous access. A *present but
invalid/expired* token is a 401. When the cookie flow is used and the
federated access token backing it is about to expire, the dependency
refreshes it server-side and re-mints the cookie (sliding session).

This module provides the core ``depends_access_token`` / ``depends_user_id``
dependencies. Permission-filter resolution (``depends_permissions``) is
deferred to the RBAC layer (built on :mod:`resourcey.auth.permission`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import Depends, HTTPException, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from resourcey.auth.auth_models import AuthToken, TokenType
from resourcey.auth.auth_tokens import InvalidTokenError, TokenService
from resourcey.auth.session import SessionDep
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.util import utc_now

_bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    auto_error=False,
    description="OAuth2 access token (JWE) sent as `Authorization: Bearer <token>`.",
)

_ACCESS_TOKEN_KEY = "_auth_access_token"
_AUTH2_ACCESS_ID_CLAIM = "aid"
_AUTH2_ACCESS_EXP_CLAIM = "axp"

_ANON_SENTINEL: Any = object()


async def depends_access_token(
    request: Request,
    response: Response,
    session: SessionDep,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
) -> AuthToken | None:
    """Resolve the current principal from a credential, or ``None``.

    The credential is a JWE-encrypted token supplied via, in priority order:

    1. the ``Authorization: Bearer <token>`` header, or
    2. the session cookie set by the login / auth callback endpoint.

    Missing token => anonymous (None). Present-but-invalid token => 401. When
    the token is the session cookie, a fresh cookie is re-minted (sliding
    session); for a federated cookie that is about to expire, the federated
    access token is refreshed server-side first.
    """
    cached = getattr(request.state, _ACCESS_TOKEN_KEY, None)
    if cached is not None:
        return cached if cached is not _ANON_SENTINEL else None

    token: str | None = None
    used_cookie = False
    if token is None and bearer is not None:
        token = bearer.credentials
    if token is None:
        cookie_name = get_config_as(FrameworkConfig).auth.cookie_name
        token = request.cookies.get(cookie_name)
        used_cookie = token is not None
    if token is None:
        _cache_token(request, _ANON_SENTINEL)
        return None

    service = TokenService(session)
    try:
        auth_token = await service.authenticate(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired auth token.",
        ) from exc

    if not auth_token.enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired auth token.",
        )

    if used_cookie:
        await _maybe_refresh_cookie(token, response, session)

    _cache_token(request, auth_token)
    return auth_token


def _cache_token(request: Request, value: AuthToken | Any) -> None:
    setattr(request.state, _ACCESS_TOKEN_KEY, value)


async def _maybe_refresh_cookie(
    token: str,
    response: Response,
    session: SessionDep,
) -> None:
    """Re-mint the session cookie, refreshing the federated access token if needed.

    A federated cookie carries an IdP access-token row id (``aid``) + expiry
    (``axp``); when that expiry is imminent the dependency triggers a
    server-side refresh before re-minting the cookie. A cookie without
    ``aid`` is a test/bootstrap token; it is re-minted off its own presented
    expiry so the no-DB path stays self-consistent.
    """
    from resourcey.auth.auth_service import AuthService, RefreshLockTimeoutError, _mint_cookie_jwe

    cfg = get_config_as(FrameworkConfig)
    enc = get_encryption_service()
    payload = enc.decrypt_jwe_token(token)
    access_id_raw = payload.get(_AUTH2_ACCESS_ID_CLAIM)
    now = utc_now()

    if not isinstance(access_id_raw, str):
        fresh = _reissue_plain_cookie(enc, payload, now)
        _set_cookie(response, fresh, _remaining_seconds(payload, now), cfg)
        return

    access_id = uuid.UUID(access_id_raw)
    access_exp = _access_exp(payload)
    drift = timedelta(seconds=cfg.auth.idp.expire_drift_tolerance)

    if access_exp is not None and access_exp > now + drift:
        fresh = _mint_cookie_jwe(
            enc,
            user_id=_user_sub(payload),
            access_id=access_id,
            access_expires_at=access_exp,
        )
        _set_cookie(response, fresh, max(1, int((access_exp - now).total_seconds())), cfg)
        return

    service = AuthService(session)
    try:
        access_row, _ = await service.refresh_access_token(access_id)
        await session.commit()
    except RefreshLockTimeoutError:
        fallback_exp = access_exp or (now + timedelta(seconds=1))
        fresh = _mint_cookie_jwe(
            enc,
            user_id=_user_sub(payload),
            access_id=access_id,
            access_expires_at=fallback_exp,
        )
        _set_cookie(response, fresh, max(1, int((fallback_exp - now).total_seconds())), cfg)
        return
    finally:
        await service.aclose()

    fresh = _mint_cookie_jwe(
        enc,
        user_id=_user_sub(payload),
        access_id=access_row.id,
        access_expires_at=access_row.expires_at,
    )
    _set_cookie(
        response,
        fresh,
        max(1, int((access_row.expires_at - now).total_seconds())),
        cfg,
    )


def _user_sub(payload: dict[str, object]) -> uuid.UUID:
    raw = payload.get("sub")
    if not isinstance(raw, str):
        raise InvalidTokenError("missing subject")
    return uuid.UUID(raw)


def _access_exp(payload: dict[str, object]) -> datetime | None:
    raw = payload.get(_AUTH2_ACCESS_EXP_CLAIM)
    if not isinstance(raw, int | float):
        return None
    return datetime.fromtimestamp(int(raw), tz=UTC)


def _payload_exp(payload: dict[str, object], now: datetime) -> datetime:
    raw = payload.get("exp")
    if isinstance(raw, int | float) and raw > 0:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    return now + timedelta(hours=1)


def _remaining_seconds(payload: dict[str, object], now: datetime) -> int:
    return max(1, int((_payload_exp(payload, now) - now).total_seconds()))


def _reissue_plain_cookie(
    enc: object,
    payload: dict[str, object],
    now: datetime,
) -> str:
    from resourcey.auth.auth_tokens import _JTI_CLAIM, _SUB_CLAIM, _TYP_CLAIM

    ttl = timedelta(seconds=_remaining_seconds(payload, now))
    sub = payload.get(_SUB_CLAIM)
    return cast(EncryptionService, enc).create_jwe_token(
        {
            _SUB_CLAIM: str(sub) if isinstance(sub, str) else "",
            _TYP_CLAIM: TokenType.COOKIE.value,
            _JTI_CLAIM: str(uuid.uuid4()),
        },
        expires_in=ttl,
    )


def _set_cookie(response: Response, value: str, max_age: int, cfg: FrameworkConfig) -> None:
    response.set_cookie(
        key=cfg.auth.cookie_name,
        value=value,
        max_age=max_age,
        httponly=True,
        samesite=cfg.auth.cookie_samesite,
        secure=cfg.auth.cookie_secure,
        path="/",
    )


async def depends_user_id(
    token: Annotated[AuthToken | None, Depends(depends_access_token)],
) -> uuid.UUID | None:
    """The current principal's user id, or ``None`` for anonymous access."""
    return token.user_id if token is not None else None


# Annotated aliases for convenient injection.
AccessToken = Annotated[AuthToken | None, Depends(depends_access_token)]
UserId = Annotated[uuid.UUID | None, Depends(depends_user_id)]
