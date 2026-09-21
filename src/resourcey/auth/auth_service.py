"""Service layer for the federated OAuth (auth) feature (issue #4).

Ported from ohev2's ``auth_service.py``, adapted to resourcey's
:class:`~resourcey.encryption.encryption_service.EncryptionService`,
:class:`~resourcey.config.config_framework.FrameworkConfig`, and
resourcey's auth models.

The project acts as a federated OAuth proxy:

1. ``/auth/authorize`` validates the client + redirect URI, redirects to IdP.
2. ``/auth/callback`` exchanges the IdP code, JIT-provisions the user,
   persists encrypted IdP tokens, mints an authorization code + session cookie.
3. ``/auth/token`` exchanges the authorization code for access + refresh tokens.
4. ``/auth/refresh`` rotates the access + refresh pair via the IdP.
5. ``/auth/revoke`` best-effort revokes a token (RFC 7009).

Access tokens are self-contained JWEs; their ``exp`` is synced to the backing
IdP access-token row's expiry. Sensitive values (IdP tokens, client secrets)
are encrypted at rest via the encryption service.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth.auth_models import (
    IdpAccessToken,
    IdpRefreshToken,
    OAuthClient,
    OAuthClientRedirectUri,
    TokenType,
    User,
    _aware,
)
from resourcey.config.config_framework import FrameworkConfig, IdpConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.encryption.encryption_service import (
    EncryptionService,
    get_encryption_service,
)

_SUB_CLAIM = "sub"
_TYP_CLAIM = "ttyp"
_JTI_CLAIM = "jti"
_CLIENT_ID_CLAIM = "cid"
_REDIRECT_URI_CLAIM = "ruri"
_STATE_CLAIM = "st"
_CODE_CHALLENGE_CLAIM = "cc"
_CODE_METHOD_CLAIM = "ccm"
_ROW_ID_CLAIM = "rid"
_ACCESS_ID_CLAIM = "aid"
_ACCESS_EXP_CLAIM = "axp"
_RESPONSE_TYPE_CLAIM = "rtyp"
_SCOPE_CLAIM = "scp"

_RESPONSE_TYPE_CODE = "code"
_RESPONSE_TYPE_COOKIE = "cookie"
_RESPONSE_TYPES = (_RESPONSE_TYPE_CODE, _RESPONSE_TYPE_COOKIE)

_AUTH_CODE_TTL = timedelta(minutes=10)
_PENDING_AUTH_TTL = timedelta(minutes=10)

_OPENID_SCOPE = "openid"
_EMAIL_SCOPE = "email"
_PROFILE_SCOPE = "profile"
_OIDC_SCOPES = frozenset({_OPENID_SCOPE, _EMAIL_SCOPE, _PROFILE_SCOPE})

_AUTH_CODE_TYP = "authorization_code"


class AuthError(Exception):
    """Base class for auth domain errors."""


class InvalidClientError(AuthError):
    """The client_id / client_secret pair is unknown or disabled."""


class InvalidRedirectUriError(AuthError):
    """The redirect_uri is not permitted for the client."""


class InvalidGrantError(AuthError):
    """The authorization code or refresh token is invalid/expired/revoked."""


class IdpError(AuthError):
    """The identity provider returned an error or an unusable response."""


class RefreshLockTimeoutError(AuthError):
    """The refresh-row lock could not be acquired within the timeout."""


def _now() -> datetime:
    return datetime.now(UTC)


class AuthService:
    """Issue, exchange, and rotate federated OAuth tokens.

    Constructed per request with the request-scoped session. The IdP HTTP
    client and encryption service are injectable for tests.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        http_client: httpx.AsyncClient | None = None,
        encryption_service: EncryptionService | None = None,
        config: FrameworkConfig | None = None,
    ) -> None:
        self._session = session
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._enc = encryption_service or get_encryption_service()
        self._cfg = config or get_config_as(FrameworkConfig)
        self._idp = self._cfg.auth.idp

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _idp_base(self) -> str:
        url = self._idp.url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"{self._cfg.base_url.rstrip('/')}/{url.lstrip('/')}"

    # ------------------------------------------------------------------ #
    # /authorize — build the IdP redirect URL.
    # ------------------------------------------------------------------ #

    async def build_authorize_redirect(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str | None,
        scope: str | None,
        code_challenge: str | None,
        code_challenge_method: str | None,
        callback_url: str,
        response_type: str = _RESPONSE_TYPE_CODE,
    ) -> str:
        if response_type not in _RESPONSE_TYPES:
            raise AuthError(f"response_type must be one of {_RESPONSE_TYPES}")
        client = await self._load_client(client_id)
        if not await self._redirect_uri_allowed(client, redirect_uri):
            raise InvalidRedirectUriError(redirect_uri)

        scopes = _normalize_scopes(scope)
        verifier = _generate_code_verifier()
        idp_challenge = _derive_code_challenge(verifier, "S256")
        idp_state = self._mint_pending_auth(
            client_id=client_id,
            redirect_uri=redirect_uri,
            client_state=state,
            scopes=scopes,
            client_code_challenge=code_challenge,
            client_code_method=code_challenge_method,
            idp_verifier=verifier,
            response_type=response_type,
        )
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._idp.client_id,
            "redirect_uri": callback_url,
            "state": idp_state,
            "scope": " ".join(self._idp.scopes),
            "code_challenge": idp_challenge,
            "code_challenge_method": "S256",
        }
        return _join_url(self._idp_base(), self._idp.authorize_path, params)

    # ------------------------------------------------------------------ #
    # /callback — exchange the IdP code, provision the user, mint our code.
    # ------------------------------------------------------------------ #

    async def handle_callback(
        self,
        *,
        code: str,
        state: str,
        callback_url: str,
    ) -> CallbackContext:
        pending = self._decode_pending_auth(state)
        idp_tokens = await self._exchange_code_with_idp(
            code=code,
            verifier=pending["idp_verifier"],
            callback_url=callback_url,
        )
        user = await self._provision_user(idp_tokens)
        refresh_row, access_row = await self.persist_idp_tokens(user.id, idp_tokens)

        response_type = pending["response_type"]
        scopes = pending["scopes"]
        auth_code: str | None = None
        if response_type == _RESPONSE_TYPE_CODE:
            auth_code = self._mint_auth_code(
                user_id=user.id,
                row_id=refresh_row.id,
                access_id=access_row.id,
                client_id=pending["client_id"],
                redirect_uri=pending["redirect_uri"],
                scopes=scopes,
                client_code_challenge=pending["client_code_challenge"],
                client_code_method=pending["client_code_method"],
            )
        return CallbackContext(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            access_expires_at=access_row.expires_at,
            client_id=pending["client_id"],
            redirect_uri=pending["redirect_uri"],
            client_state=pending["client_state"],
            response_type=response_type,
            scopes=scopes,
            auth_code=auth_code,
            expires_in=_seconds_until(access_row.expires_at),
        )

    # ------------------------------------------------------------------ #
    # /token — exchange our authorization code for access + refresh tokens.
    # ------------------------------------------------------------------ #

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str | None,
    ) -> TokenPair:
        client = await self._authenticate_client(client_id, client_secret)
        _ = client
        payload = self._decrypt(code)
        if payload.get(_TYP_CLAIM) != _AUTH_CODE_TYP:
            raise InvalidGrantError("not an authorization code")
        if payload.get(_REDIRECT_URI_CLAIM) != redirect_uri:
            raise InvalidGrantError("redirect_uri mismatch")
        self._verify_pkce(
            challenge=payload.get(_CODE_CHALLENGE_CLAIM),
            method=payload.get(_CODE_METHOD_CLAIM),
            verifier=code_verifier,
        )
        user_id = _uuid(payload, _SUB_CLAIM)
        row_id = _uuid(payload, _ROW_ID_CLAIM)
        access_id = _uuid(payload, _ACCESS_ID_CLAIM)
        scopes = _scopes_from_payload(payload)
        refresh_row, access_row = await self._load_token_rows(row_id, access_id)
        if _aware(refresh_row.expires_at) <= _now():
            raise InvalidGrantError("refresh token expired")
        return await self._mint_token_pair(
            user_id=user_id,
            refresh_row=refresh_row,
            access_row=access_row,
            scopes=scopes,
        )

    # ------------------------------------------------------------------ #
    # /refresh — rotate the access + refresh pair via the IdP.
    # ------------------------------------------------------------------ #

    async def exchange_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenPair:
        await self._authenticate_client(client_id, client_secret)
        payload = self._decrypt(refresh_token)
        if payload.get(_TYP_CLAIM) != TokenType.IDP_REFRESH_TOKEN.value:
            raise InvalidGrantError("not a refresh token")
        user_id = _uuid(payload, _SUB_CLAIM)
        row_id = _uuid(payload, _ROW_ID_CLAIM)
        scopes = _scopes_from_payload(payload)
        return await self._refresh_under_lock(user_id, row_id, scopes)

    async def refresh_access_token(self, access_id: uuid.UUID) -> tuple[IdpAccessToken, IdpRefreshToken]:
        """Refresh the federated access token for *access_id* under a row lock.

        Called by the auth dependency's cookie auto-refresh path. Loads the
        access row + its backing refresh row, acquires a ``FOR UPDATE`` lock on
        the refresh row, re-checks the expiry (a concurrent refresh may have
        already refreshed it), and if still stale performs the IdP refresh.
        Returns the new (access_row, refresh_row) for cookie re-minting.
        """
        access_row = await self._session.get(IdpAccessToken, access_id)
        if access_row is None:
            raise InvalidGrantError("access token not found")
        refresh_row = await self._session.get(IdpRefreshToken, access_row.refresh_token_id)
        if refresh_row is None:
            raise InvalidGrantError("refresh token not found")
        await self._do_refresh_under_lock(refresh_row)
        # Re-fetch the access row — it may have been replaced by the refresh.
        new_access = await self._load_access_row_for_refresh(refresh_row.id)
        if new_access is not None:
            access_row = new_access
        return access_row, refresh_row

    async def _do_refresh_under_lock(self, refresh_row: IdpRefreshToken) -> None:
        """Acquire the row lock and refresh if stale (in-place on the row)."""
        await self._set_lock_timeout()
        try:
            result = await self._session.execute(
                select(IdpRefreshToken)
                .where(IdpRefreshToken.id == refresh_row.id)
                .with_for_update()
            )
            locked_row = result.scalar_one_or_none()
        except OperationalError as exc:
            if "timeout" in str(exc).lower() or "lock" in str(exc).lower():
                raise RefreshLockTimeoutError(str(exc)) from exc
            raise
        if locked_row is None or _aware(locked_row.expires_at) <= _now():
            raise InvalidGrantError("refresh token expired or revoked")

        access_row = await self._load_access_row_for_refresh(locked_row.id)
        if access_row is not None and _aware(access_row.expires_at) > _now() + timedelta(
            seconds=self._idp.expire_drift_tolerance
        ):
            return  # A concurrent refresh already refreshed it.

        idp_tokens = await self._refresh_with_idp(locked_row)
        await self._replace_idp_tokens(
            locked_row.creator_id, idp_tokens, locked_row
        )

    async def _refresh_under_lock(
        self,
        user_id: uuid.UUID,
        row_id: uuid.UUID,
        scopes: frozenset[str],
    ) -> TokenPair:
        """Refresh the IdP token pair under a row lock (concurrent-safe)."""
        await self._set_lock_timeout()
        try:
            result = await self._session.execute(
                select(IdpRefreshToken)
                .where(IdpRefreshToken.id == row_id)
                .with_for_update()
            )
            refresh_row = result.scalar_one_or_none()
        except OperationalError as exc:
            if "timeout" in str(exc).lower() or "lock" in str(exc).lower():
                raise RefreshLockTimeoutError(str(exc)) from exc
            raise
        if refresh_row is None or _aware(refresh_row.expires_at) <= _now():
            raise InvalidGrantError("refresh token expired or revoked")

        access_row = await self._load_access_row_for_refresh(refresh_row.id)
        if access_row is not None and _aware(access_row.expires_at) > _now() + timedelta(
            seconds=self._idp.expire_drift_tolerance
        ):
            # A concurrent refresh already refreshed; re-mint from the existing rows.
            return await self._mint_token_pair(user_id, refresh_row, access_row, scopes)

        idp_tokens = await self._refresh_with_idp(refresh_row)
        new_refresh_row, new_access_row = await self._replace_idp_tokens(
            user_id, idp_tokens, refresh_row
        )
        return await self._mint_token_pair(user_id, new_refresh_row, new_access_row, scopes)

    async def _set_lock_timeout(self) -> None:
        """Set the per-session lock timeout for FOR UPDATE."""
        timeout_ms = int(self._idp.refresh_lock_timeout_seconds * 1000)
        await self._session.execute(text(f"SET LOCAL lock_timeout = {timeout_ms}"))

    # ------------------------------------------------------------------ #
    # /revoke — best-effort token revocation (RFC 7009).
    # ------------------------------------------------------------------ #

    async def revoke_token(
        self,
        *,
        token: str,
        token_type_hint: str | None,
        client_id: str,
        client_secret: str,
    ) -> None:
        if client_id:
            try:
                await self._authenticate_client(client_id, client_secret)
            except InvalidClientError:
                raise
        try:
            payload = self._enc.decrypt_jwe_token(token)
        except Exception:
            return  # best-effort

        token_type = payload.get(_TYP_CLAIM)
        if token_type == TokenType.IDP_REFRESH_TOKEN.value:
            row_id = _uuid(payload, _ROW_ID_CLAIM)
            await self._revoke_refresh_token(row_id)
        # Access-token revocation is best-effort (JWE remains usable until exp).

    async def revoke_session(self, cookie_token: str) -> None:
        """Revoke the federated session backing a session cookie."""
        try:
            payload = self._enc.decrypt_jwe_token(cookie_token)
        except Exception:
            return  # best-effort
        access_id_raw = payload.get(_ACCESS_ID_CLAIM)
        if not isinstance(access_id_raw, str):
            return
        access_id = uuid.UUID(access_id_raw)
        access_row = await self._session.get(IdpAccessToken, access_id)
        if access_row is None:
            return
        refresh_row = await self._session.get(IdpRefreshToken, access_row.refresh_token_id)
        if refresh_row is not None:
            await self._revoke_refresh_token(refresh_row.id)

    async def _revoke_refresh_token(self, row_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(IdpRefreshToken).where(IdpRefreshToken.id == row_id)
        )

    # ------------------------------------------------------------------ #
    # Persist / replace IdP tokens.
    # ------------------------------------------------------------------ #

    async def persist_idp_tokens(
        self,
        user_id: uuid.UUID,
        idp_tokens: dict[str, Any],
    ) -> tuple[IdpRefreshToken, IdpAccessToken]:
        """Persist encrypted IdP refresh + access token rows for *user_id*."""
        refresh_row = IdpRefreshToken(
            creator_id=user_id,
            refresh_token=self._enc.encrypt_value(idp_tokens["refresh_token"]),
            expires_at=_idp_refresh_expiry(idp_tokens, self._idp.expire_drift_tolerance, self._idp),
        )
        self._session.add(refresh_row)
        await self._session.flush()
        access_row = IdpAccessToken(
            refresh_token_id=refresh_row.id,
            access_token=self._enc.encrypt_value(idp_tokens["access_token"]),
            expires_at=_idp_access_expiry(idp_tokens, self._idp.expire_drift_tolerance, self._idp),
        )
        self._session.add(access_row)
        await self._session.flush()
        return refresh_row, access_row

    async def _replace_idp_tokens(
        self,
        user_id: uuid.UUID,
        idp_tokens: dict[str, Any],
        old_refresh_row: IdpRefreshToken,
    ) -> tuple[IdpRefreshToken, IdpAccessToken]:
        """Delete old rows and persist new ones (refresh rotation)."""
        await self._session.delete(old_refresh_row)
        await self._session.flush()
        return await self.persist_idp_tokens(user_id, idp_tokens)

    # ------------------------------------------------------------------ #
    # Userinfo / discovery.
    # ------------------------------------------------------------------ #

    async def build_userinfo_claims(
        self,
        user_id: uuid.UUID,
        scopes: frozenset[str],
    ) -> dict[str, Any] | None:
        user = await self._session.get(User, user_id)
        if user is None or not user.enabled:
            return None
        claims: dict[str, Any] = {"sub": str(user.id)}
        if _EMAIL_SCOPE in scopes:
            claims["email"] = user.email
            claims["email_verified"] = True
        if _PROFILE_SCOPE in scopes:
            claims["name"] = user.username
            claims["preferred_username"] = user.username
        return claims

    def build_discovery_document(self) -> dict[str, Any]:
        base = self._cfg.base_url.rstrip("/")
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/auth/authorize",
            "token_endpoint": f"{base}/auth/token",
            "userinfo_endpoint": f"{base}/auth/userinfo",
            "revocation_endpoint": f"{base}/auth/revoke",
            "response_types_supported": ["code", "cookie"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "subject_types_supported": ["public"],
            "scopes_supported": list(_OIDC_SCOPES),
        }

    # ------------------------------------------------------------------ #
    # OAuth client CRUD.
    # ------------------------------------------------------------------ #

    async def get_client_by_client_id(self, client_id: str) -> OAuthClient | None:
        result = await self._session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
        return result.scalar_one_or_none()

    async def list_redirect_uris(self, client: OAuthClient) -> list[str]:
        result = await self._session.execute(
            select(OAuthClientRedirectUri.uri).where(
                OAuthClientRedirectUri.client_id == client.id
            )
        )
        return list(result.scalars().all())

    async def create_oauth_client(
        self,
        *,
        client_id: str,
        client_secret: str,
        name: str | None = None,
        redirect_uris: list[str] | None = None,
        enabled: bool = True,
    ) -> OAuthClient:
        client = OAuthClient(
            client_id=client_id,
            client_secret=self._enc.encrypt_value(client_secret),
            name=name,
            enabled=enabled,
        )
        self._session.add(client)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise InvalidClientError(f"client_id already exists: {client_id}") from exc
        for uri in redirect_uris or []:
            self._session.add(OAuthClientRedirectUri(client_id=client.id, uri=uri))
        await self._session.flush()
        return client

    # ------------------------------------------------------------------ #
    # Internals — IdP HTTP calls.
    # ------------------------------------------------------------------ #

    async def _exchange_code_with_idp(
        self,
        *,
        code: str,
        verifier: str,
        callback_url: str,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url,
            "client_id": self._idp.client_id,
            "client_secret": self._idp.client_secret.get_secret_value(),
            "code_verifier": verifier,
        }
        return await self._idp_token_post(data)

    async def _refresh_with_idp(self, refresh_row: IdpRefreshToken) -> dict[str, Any]:
        refresh_token = self._enc.decrypt_value(refresh_row.refresh_token)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._idp.client_id,
            "client_secret": self._idp.client_secret.get_secret_value(),
        }
        return await self._idp_token_post(data)

    async def _idp_token_post(self, data: dict[str, str]) -> dict[str, Any]:
        url = _join_url(self._idp_base(), self._idp.token_path)
        resp = await self._http.post(url, data=data)
        if resp.status_code != 200:
            raise IdpError(f"IdP token endpoint returned {resp.status_code}: {resp.text}")
        body: dict[str, Any] = resp.json()
        if "access_token" not in body or "refresh_token" not in body:
            raise IdpError("IdP response missing access_token or refresh_token")
        return body

    # ------------------------------------------------------------------ #
    # Internals — user provisioning (JIT).
    # ------------------------------------------------------------------ #

    async def _provision_user(self, idp_tokens: dict[str, Any]) -> User:
        id_token = idp_tokens.get("id_token")
        claims: dict[str, Any] = {}
        if isinstance(id_token, str):
            claims = _decode_id_token(id_token)
        sub = _claim(claims, None, "sub") or ""
        email = _claim(claims, None, "email") or ""
        if not sub and not email:
            raise IdpError("IdP id_token missing sub and email")
        user = await self._find_user_by_idp_sub(sub) if sub else None
        if user is None and email:
            user = await self._find_user_by_email(email)
        if user is None:
            if not email:
                raise IdpError("IdP id_token missing email for new user provisioning")
            username = email.split("@")[0]
            user = User(email=email, username=username, idp_user_id=sub or None)
            self._session.add(user)
            await self._session.flush()
        elif sub and user.idp_user_id is None:
            user.idp_user_id = sub
            await self._session.flush()
        return user

    async def _find_user_by_idp_sub(self, sub: str) -> User | None:
        result = await self._session.execute(select(User).where(User.idp_user_id == sub))
        return result.scalar_one_or_none()

    async def _find_user_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Internals — token minting.
    # ------------------------------------------------------------------ #

    async def _mint_token_pair(
        self,
        user_id: uuid.UUID,
        refresh_row: IdpRefreshToken,
        access_row: IdpAccessToken,
        scopes: frozenset[str],
    ) -> TokenPair:
        from resourcey.auth.auth_tokens import TokenService

        token_service = TokenService(
            self._session, encryption_service=self._enc, config=self._cfg
        )
        access_token = token_service._mint_access_token(user_id, access_row)
        refresh_token = token_service._mint_refresh_token(user_id, refresh_row)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=_seconds_until(access_row.expires_at),
            refresh_expires_at=refresh_row.expires_at,
            access_expires_at=access_row.expires_at,
        )

    def _mint_auth_code(
        self,
        *,
        user_id: uuid.UUID,
        row_id: uuid.UUID,
        access_id: uuid.UUID,
        client_id: str,
        redirect_uri: str,
        scopes: frozenset[str],
        client_code_challenge: str | None,
        client_code_method: str | None,
    ) -> str:
        payload: dict[str, Any] = {
            _SUB_CLAIM: str(user_id),
            _TYP_CLAIM: _AUTH_CODE_TYP,
            _JTI_CLAIM: str(uuid.uuid4()),
            _ROW_ID_CLAIM: str(row_id),
            _ACCESS_ID_CLAIM: str(access_id),
            _CLIENT_ID_CLAIM: client_id,
            _REDIRECT_URI_CLAIM: redirect_uri,
        }
        if scopes:
            payload[_SCOPE_CLAIM] = " ".join(sorted(scopes))
        if client_code_challenge is not None:
            payload[_CODE_CHALLENGE_CLAIM] = client_code_challenge
            payload[_CODE_METHOD_CLAIM] = client_code_method or "plain"
        return self._enc.create_jwe_token(payload, expires_in=_AUTH_CODE_TTL)

    # ------------------------------------------------------------------ #
    # Internals — pending-auth state (IdP state param).
    # ------------------------------------------------------------------ #

    def _mint_pending_auth(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        client_state: str | None,
        scopes: frozenset[str],
        client_code_challenge: str | None,
        client_code_method: str | None,
        idp_verifier: str,
        response_type: str,
    ) -> str:
        payload: dict[str, Any] = {
            _CLIENT_ID_CLAIM: client_id,
            _REDIRECT_URI_CLAIM: redirect_uri,
            _RESPONSE_TYPE_CLAIM: response_type,
            "ivf": idp_verifier,
        }
        if client_state is not None:
            payload[_STATE_CLAIM] = client_state
        if scopes:
            payload[_SCOPE_CLAIM] = " ".join(sorted(scopes))
        if client_code_challenge is not None:
            payload[_CODE_CHALLENGE_CLAIM] = client_code_challenge
            payload[_CODE_METHOD_CLAIM] = client_code_method or "plain"
        return self._enc.create_jwe_token(payload, expires_in=_PENDING_AUTH_TTL)

    def _decode_pending_auth(self, state: str) -> dict[str, Any]:
        payload = self._decrypt(state)
        if payload.get("ivf") is None or payload.get(_CLIENT_ID_CLAIM) is None:
            raise InvalidGrantError("invalid state")
        response_type = payload.get(_RESPONSE_TYPE_CLAIM) or _RESPONSE_TYPE_CODE
        return {
            "client_id": str(payload[_CLIENT_ID_CLAIM]),
            "redirect_uri": str(payload[_REDIRECT_URI_CLAIM]),
            "client_state": payload.get(_STATE_CLAIM),
            "client_code_challenge": payload.get(_CODE_CHALLENGE_CLAIM),
            "client_code_method": payload.get(_CODE_METHOD_CLAIM),
            "response_type": response_type,
            "scopes": _scopes_from_payload(payload),
            "idp_verifier": str(payload["ivf"]),
        }

    # ------------------------------------------------------------------ #
    # Internals — client / redirect_uri validation.
    # ------------------------------------------------------------------ #

    async def _load_client(self, client_id: str) -> OAuthClient:
        client = await self.get_client_by_client_id(client_id)
        if client is None or not client.enabled:
            raise InvalidClientError(client_id)
        return client

    async def _authenticate_client(
        self,
        client_id: str,
        client_secret: str,
    ) -> OAuthClient:
        client = await self.get_client_by_client_id(client_id)
        if client is None or not client.enabled:
            raise InvalidClientError(client_id)
        try:
            stored = self._enc.decrypt_value(client.client_secret)
        except Exception as exc:
            raise InvalidClientError(client_id) from exc
        if not secrets.compare_digest(stored, client_secret):
            raise InvalidClientError(client_id)
        return client

    async def _redirect_uri_allowed(
        self,
        client: OAuthClient,
        redirect_uri: str,
    ) -> bool:
        result = await self._session.execute(
            select(OAuthClientRedirectUri.uri).where(
                OAuthClientRedirectUri.client_id == client.id
            )
        )
        patterns = list(result.scalars().all())
        return any(_wildcard_match(p, redirect_uri) for p in patterns)

    # ------------------------------------------------------------------ #
    # Internals — JWE / row helpers.
    # ------------------------------------------------------------------ #

    def _decrypt(self, token: str) -> dict[str, Any]:
        try:
            payload = self._enc.decrypt_jwe_token(token)
        except Exception as exc:
            raise InvalidGrantError("token decryption failed") from exc
        if not isinstance(payload, dict):
            raise InvalidGrantError("invalid token payload")
        return payload

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

    async def _load_token_rows(
        self,
        row_id: uuid.UUID,
        access_id: uuid.UUID,
    ) -> tuple[IdpRefreshToken, IdpAccessToken]:
        refresh_row = await self._session.get(IdpRefreshToken, row_id)
        if refresh_row is None:
            raise InvalidGrantError("refresh token not found")
        access_row = await self._session.get(IdpAccessToken, access_id)
        if access_row is None:
            raise InvalidGrantError("access token not found")
        return refresh_row, access_row

    async def _load_access_row_for_refresh(
        self,
        refresh_row_id: uuid.UUID,
    ) -> IdpAccessToken | None:
        result = await self._session.execute(
            select(IdpAccessToken)
            .where(IdpAccessToken.refresh_token_id == refresh_row_id)
            .order_by(IdpAccessToken.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------- #
# Value objects.
# ---------------------------------------------------------------------- #


class CallbackContext:
    """The result of ``/auth/callback``: client redirect + token context."""

    __slots__ = (
        "access_expires_at",
        "access_id",
        "auth_code",
        "client_id",
        "client_state",
        "expires_in",
        "redirect_uri",
        "response_type",
        "row_id",
        "scopes",
        "user_id",
    )

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        row_id: uuid.UUID,
        access_id: uuid.UUID,
        access_expires_at: datetime,
        client_id: str,
        redirect_uri: str,
        client_state: str | None,
        response_type: str,
        scopes: frozenset[str],
        auth_code: str | None,
        expires_in: int,
    ) -> None:
        self.user_id = user_id
        self.row_id = row_id
        self.access_id = access_id
        self.access_expires_at = access_expires_at
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.client_state = client_state
        self.response_type = response_type
        self.scopes = scopes
        self.auth_code = auth_code
        self.expires_in = expires_in


class TokenPair:
    """An access + refresh token pair with synced federated expiries."""

    __slots__ = (
        "access_expires_at",
        "access_token",
        "expires_in",
        "id_token",
        "refresh_expires_at",
        "refresh_token",
    )

    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        refresh_expires_at: datetime,
        access_expires_at: datetime,
        id_token: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.refresh_expires_at = refresh_expires_at
        self.access_expires_at = access_expires_at
        self.id_token = id_token


# ---------------------------------------------------------------------- #
# Module-level helpers.
# ---------------------------------------------------------------------- #


def _claim(claims: dict[str, Any], configured: str | None, default: str) -> str | None:
    key = configured or default
    value = claims.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _normalize_scopes(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    requested = {s for s in raw.split() if s}
    return frozenset(requested & _OIDC_SCOPES)


def _scopes_from_payload(payload: dict[str, Any]) -> frozenset[str]:
    raw = payload.get(_SCOPE_CLAIM)
    if not isinstance(raw, str):
        return frozenset()
    return _normalize_scopes(raw)


def _uuid(payload: dict[str, Any], key: str) -> uuid.UUID:
    raw = payload.get(key)
    if not isinstance(raw, str):
        raise InvalidGrantError(f"missing {key}")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise InvalidGrantError(f"invalid {key}") from exc


def _idp_access_expiry(
    idp_tokens: dict[str, Any],
    drift_seconds: int,
    idp: IdpConfig,
) -> datetime:
    expires_in = idp_tokens.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        return _now() + timedelta(seconds=max(0, int(expires_in) - drift_seconds))
    expires_at = idp_tokens.get("expires_at")
    if isinstance(expires_at, int | float) and expires_at > 0:
        return datetime.fromtimestamp(max(0, int(expires_at) - drift_seconds), tz=UTC)
    return _now() + timedelta(seconds=max(1, idp.access_token_expires_in - drift_seconds))


def _idp_refresh_expiry(
    idp_tokens: dict[str, Any],
    drift_seconds: int,
    idp: IdpConfig,
) -> datetime:
    refresh_expires_in = idp_tokens.get("refresh_expires_in")
    if isinstance(refresh_expires_in, int | float) and refresh_expires_in > 0:
        return _now() + timedelta(seconds=max(0, int(refresh_expires_in) - drift_seconds))
    refresh_expires_at = idp_tokens.get("refresh_expires_at")
    if isinstance(refresh_expires_at, int | float) and refresh_expires_at > 0:
        return datetime.fromtimestamp(max(0, int(refresh_expires_at) - drift_seconds), tz=UTC)
    return _now() + timedelta(seconds=max(1, idp.refresh_token_expires_in - drift_seconds))


def _seconds_until(expires_at: datetime) -> int:
    return max(0, int((_aware(expires_at) - _now()).total_seconds()))


def _mint_cookie_jwe(
    enc: EncryptionService,
    *,
    user_id: uuid.UUID,
    access_id: uuid.UUID,
    access_expires_at: datetime,
) -> str:
    """Mint a session-cookie JWE synced to an IdP access-token row."""
    ttl = _aware(access_expires_at) - _now()
    if ttl.total_seconds() <= 0:
        ttl = timedelta(seconds=1)
    return enc.create_jwe_token(
        {
            _SUB_CLAIM: str(user_id),
            _TYP_CLAIM: TokenType.COOKIE.value,
            _JTI_CLAIM: str(uuid.uuid4()),
            _ACCESS_ID_CLAIM: str(access_id),
            _ACCESS_EXP_CLAIM: int(_aware(access_expires_at).timestamp()),
        },
        expires_in=ttl,
    )


def _decode_id_token(id_token: str) -> dict[str, Any]:
    """Decode an id_token's payload without verifying its signature."""
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        import json

        data: Any = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _derive_code_challenge(verifier: str, method: str) -> str:
    if method.upper() == "S256":
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier


def _wildcard_match(pattern: str, target: str) -> bool:
    if "*" not in pattern:
        return secrets.compare_digest(pattern, target)
    parts = pattern.split("*")
    escaped = ".*".join(re.escape(p) for p in parts)
    return re.fullmatch(escaped, target) is not None


def _join_url(base: str, path: str, params: dict[str, str] | None = None) -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if params:
        url = f"{url}?{urlencode(params)}"
    return url
