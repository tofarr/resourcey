"""Built-in dev identity provider mounted at ``/auth/dev`` (issue #4).

Ported from ohev2's ``dev_router.py``, adapted to resourcey.

Selected by setting ``idp.url = "/auth/dev"`` (the default). This router acts
as a minimal OAuth2 identity provider so the system works out of the box
without configuring an external IdP:

* ``GET  /auth/dev/authorize`` — HTTP Basic auth challenge; on success mints
  an authorization code and redirects to the project's OAuth callback.
* ``POST /auth/dev/token``    — exchanges the code (or rotates a refresh
  token) for an IdP access + refresh token pair plus an ``id_token``.
* ``POST /auth/dev/refresh``  — dedicated refresh endpoint.
* ``POST /auth/dev/login``    — dev-only username/password login that sets a
  session cookie directly (for the OpenAPI docs page).

It is **not** a production IdP — it exists so users can try the system before
wiring up a real identity provider.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth.auth_models import User
from resourcey.auth.auth_schemas import DevLoginRequest
from resourcey.auth.auth_service import (
    AuthService,
    _derive_code_challenge,
    _join_url,
    _mint_cookie_jwe,
)
from resourcey.auth.password import verify_password
from resourcey.auth.session import SessionDep
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.util import utc_now

_SUB_CLAIM = "sub"
_TYP_CLAIM = "ttyp"
_JTI_CLAIM = "jti"
_EMAIL_CLAIM = "email"
_CLIENT_ID_CLAIM = "cid"
_REDIRECT_URI_CLAIM = "ruri"
_CODE_CHALLENGE_CLAIM = "cc"
_CODE_METHOD_CLAIM = "ccm"
_STATE_CLAIM = "st"

_DEV_CODE_TYP = "dev_authorization_code"
_DEV_ACCESS_TYP = "dev_access_token"
_DEV_REFRESH_TYP = "dev_refresh_token"

_DEV_CODE_TTL = timedelta(minutes=10)

_basic_scheme = HTTPBasic(auto_error=False, scheme_name="DevIdpBasic")


class DevIdpError(Exception):
    """Base class for dev IdP domain errors."""


class InvalidClientError(DevIdpError):
    """The client_id / client_secret pair does not match the configured IdP client."""


class InvalidGrantError(DevIdpError):
    """The authorization code or refresh token is invalid / expired / revoked."""


class InvalidRedirectUriError(DevIdpError):
    """The redirect_uri does not match the project's OAuth callback URL."""


def _seconds_until(expires_at: datetime) -> int:
    return max(0, int((expires_at - utc_now()).total_seconds()))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_id_token(sub: str, email: str) -> str:
    """Build an unsigned (``alg=none``) id_token carrying ``sub`` + ``email``."""
    header = _b64url(json.dumps({"alg": "none"}).encode())
    payload = _b64url(json.dumps({"sub": sub, "email": email}).encode())
    return f"{header}.{payload}."


class DevIdpService:
    """Issue, exchange, and rotate the dev IdP's OAuth tokens.

    Tokens are self-contained JWEs (no DB rows): the code carries the user
    identity + PKCE challenge, and the refresh token carries the user id so
    ``/refresh`` can mint a successor pair without re-authenticating.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        encryption_service: EncryptionService | None = None,
        config: FrameworkConfig | None = None,
    ) -> None:
        self._session = session
        self._enc = encryption_service or get_encryption_service()
        self._cfg = config or get_config_as(FrameworkConfig)
        self._idp = self._cfg.auth.idp

    async def login(
        self,
        *,
        username: str,
        password: str,
    ) -> tuple[User, dict[str, Any]]:
        """Authenticate a user directly with username + password."""
        user = await self._authenticate_user(username, password)
        return user, self._token_response(user.id, user.email)

    async def authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str | None,
        code_challenge: str | None,
        code_challenge_method: str | None,
        username: str,
        password: str,
    ) -> str:
        """Validate the client + user and return the callback redirect URL."""
        self._validate_client(client_id, None)
        self._validate_redirect_uri(redirect_uri)
        user = await self._authenticate_user(username, password)

        code = self._mint_code(
            user_id=user.id,
            email=user.email,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        params: dict[str, str] = {"code": code}
        if state is not None:
            params["state"] = state
        return _join_url(redirect_uri, "", params)

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str | None,
    ) -> dict[str, Any]:
        """Exchange an authorization code for an IdP access + refresh pair."""
        self._validate_client(client_id, client_secret)
        payload = self._decrypt(code)
        if payload.get(_TYP_CLAIM) != _DEV_CODE_TYP:
            raise InvalidGrantError("not an authorization code")
        if payload.get(_REDIRECT_URI_CLAIM) != redirect_uri:
            raise InvalidGrantError("redirect_uri mismatch")
        self._verify_pkce(
            challenge=payload.get(_CODE_CHALLENGE_CLAIM),
            method=payload.get(_CODE_METHOD_CLAIM),
            verifier=code_verifier,
        )
        user_id = self._user_id(payload)
        email = self._email(payload)
        return self._token_response(user_id, email)

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        """Rotate the IdP access + refresh pair from a refresh token."""
        self._validate_client(client_id, client_secret)
        payload = self._decrypt(refresh_token)
        if payload.get(_TYP_CLAIM) != _DEV_REFRESH_TYP:
            raise InvalidGrantError("not a refresh token")
        user_id = self._user_id(payload)
        email = self._email(payload)
        return self._token_response(user_id, email)

    def _validate_client(self, client_id: str, client_secret: str | None) -> None:
        if not secrets.compare_digest(client_id, self._idp.client_id):
            raise InvalidClientError(client_id)
        if client_secret is not None and not secrets.compare_digest(
            client_secret, self._idp.client_secret.get_secret_value()
        ):
            raise InvalidClientError(client_id)

    def _validate_redirect_uri(self, redirect_uri: str) -> None:
        expected = f"{self._cfg.base_url.rstrip('/')}/auth/callback"
        if redirect_uri != expected:
            raise InvalidRedirectUriError(redirect_uri)

    async def _authenticate_user(self, username: str, password: str) -> User:
        result = await self._session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None or not user.enabled or not user.password:
            raise InvalidGrantError("invalid credentials")
        if not verify_password(password, user.password):
            raise InvalidGrantError("invalid credentials")
        return user

    def _verify_pkce(
        self,
        *,
        challenge: Any,
        method: Any,
        verifier: str | None,
    ) -> None:
        if challenge is None:
            return
        if verifier is None:
            raise InvalidGrantError("missing code_verifier")
        m = method or "plain"
        expected = _derive_code_challenge(verifier, m)
        if not secrets.compare_digest(expected, str(challenge)):
            raise InvalidGrantError("PKCE verification failed")

    def _token_response(self, user_id: uuid.UUID, email: str) -> dict[str, Any]:
        access_ttl = timedelta(seconds=max(1, self._idp.access_token_expires_in))
        refresh_ttl = timedelta(seconds=max(1, self._idp.refresh_token_expires_in))
        access_exp = utc_now() + access_ttl
        refresh_exp = utc_now() + refresh_ttl
        access = self._mint(_DEV_ACCESS_TYP, user_id, access_ttl)
        refresh = self._mint(_DEV_REFRESH_TYP, user_id, refresh_ttl, email=email)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": _seconds_until(access_exp),
            "refresh_expires_in": _seconds_until(refresh_exp),
            "id_token": _make_id_token(str(user_id), email),
        }

    def _mint(
        self,
        token_type: str,
        user_id: uuid.UUID,
        expires_in: timedelta,
        *,
        email: str | None = None,
    ) -> str:
        claims: dict[str, Any] = {
            _SUB_CLAIM: str(user_id),
            _TYP_CLAIM: token_type,
            _JTI_CLAIM: str(uuid.uuid4()),
        }
        if email is not None:
            claims[_EMAIL_CLAIM] = email
        return self._enc.create_jwe_token(claims, expires_in=expires_in)

    def _mint_code(
        self,
        *,
        user_id: uuid.UUID,
        email: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str | None,
        code_challenge_method: str | None,
    ) -> str:
        payload: dict[str, Any] = {
            _SUB_CLAIM: str(user_id),
            _TYP_CLAIM: _DEV_CODE_TYP,
            _JTI_CLAIM: str(uuid.uuid4()),
            _EMAIL_CLAIM: email,
            _CLIENT_ID_CLAIM: client_id,
            _REDIRECT_URI_CLAIM: redirect_uri,
        }
        if code_challenge is not None:
            payload[_CODE_CHALLENGE_CLAIM] = code_challenge
            payload[_CODE_METHOD_CLAIM] = code_challenge_method or "plain"
        return self._enc.create_jwe_token(payload, expires_in=_DEV_CODE_TTL)

    def _decrypt(self, token: str) -> dict[str, Any]:
        try:
            payload = self._enc.decrypt_jwe_token(token)
        except Exception as exc:
            raise InvalidGrantError("token decryption failed") from exc
        if not isinstance(payload, dict):
            raise InvalidGrantError("invalid token payload")
        return payload

    def _user_id(self, payload: dict[str, Any]) -> uuid.UUID:
        raw = payload.get(_SUB_CLAIM)
        if not isinstance(raw, str):
            raise InvalidGrantError("missing subject")
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise InvalidGrantError("invalid subject") from exc

    def _email(self, payload: dict[str, Any]) -> str:
        raw = payload.get(_EMAIL_CLAIM)
        if not isinstance(raw, str) or not raw:
            raise InvalidGrantError("missing email")
        return raw


router = APIRouter(prefix="/auth/dev", tags=["auth-dev"])


def _basic_auth_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing dev IdP credentials.",
        headers={"WWW-Authenticate": 'Basic realm="resourcey-dev-idp"'},
    )


async def _dev_idp_dep(session: SessionDep) -> DevIdpService:
    return DevIdpService(session)


DevIdpDep = Annotated[DevIdpService, Depends(_dev_idp_dep)]


def _set_dev_session_cookie(
    response: Response,
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
        max_age=max(1, int((access_expires_at - utc_now()).total_seconds())),
        httponly=True,
        samesite=cfg.auth.cookie_samesite,
        secure=cfg.auth.cookie_secure,
        path="/",
    )


@router.post("/login")
async def login(
    service: DevIdpDep,
    session: SessionDep,
    response: Response,
    payload: DevLoginRequest,
) -> dict[str, str]:
    """Dev-only username/password login that sets a session cookie."""
    try:
        user, idp_tokens = await service.login(
            username=payload.username,
            password=payload.password,
        )
    except InvalidGrantError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        ) from None
    auth_service = AuthService(session)
    try:
        _, access_row = await auth_service.persist_idp_tokens(user.id, idp_tokens)
    finally:
        await auth_service.aclose()
    await session.commit()
    _set_dev_session_cookie(
        response,
        user_id=user.id,
        access_id=access_row.id,
        access_expires_at=access_row.expires_at,
    )
    return {"user_id": str(user.id), "username": user.username}


@router.get("/authorize")
async def authorize(
    service: DevIdpDep,
    response_type: Annotated[str, Query()],
    client_id: Annotated[str, Query()],
    redirect_uri: Annotated[str, Query()],
    state: Annotated[str | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
    code_challenge: Annotated[str | None, Query()] = None,
    code_challenge_method: Annotated[str | None, Query()] = None,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)] = None,
) -> RedirectResponse:
    """Dev IdP authorization endpoint (HTTP Basic auth challenge)."""
    _ = scope
    if credentials is None:
        raise _basic_auth_unauthorized()
    try:
        location = await service.authorize(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            username=credentials.username,
            password=credentials.password,
        )
    except InvalidClientError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except InvalidRedirectUriError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidGrantError:
        raise _basic_auth_unauthorized() from None
    return RedirectResponse(url=location, status_code=status.HTTP_302_FOUND)


@router.post("/token")
async def token(
    service: DevIdpDep,
    grant_type: Annotated[str, Form()],
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Dev IdP token endpoint (RFC 6749 §4.1.3 + §6)."""
    return await _handle_token(
        service,
        grant_type=grant_type,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        code_verifier=code_verifier,
        refresh_token=refresh_token,
    )


@router.post("/refresh")
async def refresh(
    service: DevIdpDep,
    grant_type: Annotated[str, Form()] = "refresh_token",
    refresh_token: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Dev IdP refresh endpoint."""
    if grant_type != "refresh_token":
        grant_type = "refresh_token"
    return await _handle_token(
        service,
        grant_type=grant_type,
        code=None,
        redirect_uri=None,
        client_id=client_id,
        client_secret=client_secret,
        code_verifier=None,
        refresh_token=refresh_token,
    )


async def _handle_token(
    service: DevIdpService,
    *,
    grant_type: str,
    code: str | None,
    redirect_uri: str | None,
    client_id: str | None,
    client_secret: str | None,
    code_verifier: str | None,
    refresh_token: str | None,
) -> dict[str, Any]:
    try:
        if grant_type == "authorization_code":
            if code is None or redirect_uri is None or client_id is None or client_secret is None:
                raise InvalidGrantError("missing authorization_code parameters")
            return await service.exchange_code(
                code=code,
                redirect_uri=redirect_uri,
                client_id=client_id,
                client_secret=client_secret,
                code_verifier=code_verifier,
            )
        if grant_type == "refresh_token":
            if refresh_token is None or client_id is None or client_secret is None:
                raise InvalidGrantError("missing refresh_token parameters")
            return await service.refresh(
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
        raise InvalidGrantError("grant_type must be 'authorization_code' or 'refresh_token'")
    except InvalidClientError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except InvalidGrantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidRedirectUriError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
