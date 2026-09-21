"""Token issuance, validation, and rotation for the auth feature (issue #4).

Ported from ohev2's ``auth_tokens.py``, adapted to resourcey's
:class:`~resourcey.encryption.encryption_service.EncryptionService` (which now
provides ``create_jwe_token`` / ``decrypt_jwe_token``) and
:class:`~resourcey.config.config_framework.FrameworkConfig`.

:meth:`TokenService.authenticate` is the single entry point that converts a
JWE-encoded token string into an :class:`AuthToken`. Per-type validity:

* COOKIE / ACCESS_TOKEN — derived from the JWE claims alone; ``enabled`` is
  the user row's ``enabled`` flag.
* API_KEY — the ``oh_``-prefixed raw key's SHA-256 hash must match a live
  ``api_keys`` row, and the user must be enabled.
* IDP_REFRESH_TOKEN — the token's ``rid`` must match a live
  ``idp_refresh_tokens`` row, and the user must be enabled. (Refresh tokens
  are exchange-only; not accepted by ``authenticate`` for general requests.)
"""

from __future__ import annotations

import hashlib
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth.auth_models import (
    ApiKey,
    AuthToken,
    IdpAccessToken,
    IdpRefreshToken,
    TokenType,
    User,
    _aware,
)
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.util import utc_now

_SUB_CLAIM = "sub"
_TYP_CLAIM = "ttyp"
_JTI_CLAIM = "jti"
_IAT_CLAIM = "iat"
_EXP_CLAIM = "exp"
_SCOPE_CLAIM = "scp"
_ACCESS_ID_CLAIM = "aid"
_ACCESS_EXP_CLAIM = "axp"
_ROW_ID_CLAIM = "rid"

_FLOOR_TTL = timedelta(seconds=1)

_API_KEY_PREFIX = "oh_"
_API_KEY_RANDOM_BITS = 128
_AMBIGUOUS = set("0Oo1IlB8S5")
_BASE52_ALPHABET = "".join(
    c
    for c in (string.digits + string.ascii_lowercase + string.ascii_uppercase)
    if c not in _AMBIGUOUS
)
assert len(_BASE52_ALPHABET) == 52
_API_KEY_PREFIX_DISPLAY_LEN = 7


class InvalidTokenError(Exception):
    """Raised when a token string cannot be authenticated."""


def _base52_encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    if num == 0:
        return _BASE52_ALPHABET[0]
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 52)
        chars.append(_BASE52_ALPHABET[rem])
    return "".join(reversed(chars))


def _generate_api_key_value() -> str:
    raw = secrets.token_bytes(_API_KEY_RANDOM_BITS // 8)
    return _API_KEY_PREFIX + _base52_encode(raw)


def hash_api_key_value(raw: str) -> str:
    """SHA-256 hex digest of the raw key value (stored for auth-time lookup)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _api_key_display_prefix(raw: str) -> str:
    return raw[:_API_KEY_PREFIX_DISPLAY_LEN]


def _scopes_from_payload(payload: dict[str, object]) -> frozenset[str]:
    raw = payload.get(_SCOPE_CLAIM)
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(s for s in raw.split() if s)


class TokenService:
    """Issue and validate JWE auth tokens, synced to federated IdP grants.

    Constructed per request with the request-scoped session. Encryption and
    config come from singletons, injectable for tests.
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

    async def create_access_token(self, user_id: uuid.UUID) -> str:
        """Mint an ACCESS_TOKEN JWE synced to the user's IdP access-token row."""
        access_row = await self._load_access_row(user_id)
        return self._mint_access_token(user_id, access_row)

    async def create_cookie_token(self, user_id: uuid.UUID) -> str:
        """Mint a session COOKIE JWE synced to the user's IdP access-token row."""
        access_row = await self._load_access_row(user_id)
        return self._mint_cookie_token(user_id, access_row)

    async def reissue_cookie(self, user_id: uuid.UUID) -> str:
        return await self.create_cookie_token(user_id)

    async def create_refresh_token(self, user_id: uuid.UUID) -> tuple[str, uuid.UUID]:
        """Mint an IDP_REFRESH_TOKEN JWE synced to the user's IdP refresh row."""
        refresh_row = await self._load_refresh_row_for_user(user_id)
        token = self._mint_refresh_token(user_id, refresh_row)
        return token, refresh_row.id

    async def create_api_key(
        self,
        user_id: uuid.UUID,
        *,
        name: str | None = None,
        enabled: bool = True,
        expires_at: datetime | None = None,
        system: bool = False,
    ) -> tuple[str, ApiKey]:
        """Mint a long-lived API key and persist its backing row."""
        raw_key = _generate_api_key_value()
        row = ApiKey(
            key_hash=hash_api_key_value(raw_key),
            prefix=_api_key_display_prefix(raw_key),
            creator_id=user_id,
            name=name,
            enabled=enabled,
            expires_at=expires_at,
            system=system,
        )
        self._session.add(row)
        await self._session.flush()
        return raw_key, row

    async def authenticate(
        self,
        token: str,
        *,
        allow_refresh: bool = False,
    ) -> AuthToken:
        """Resolve *token* into a validated :class:`AuthToken`."""
        if token.startswith(_API_KEY_PREFIX):
            return await self._authenticate_api_key(token)

        payload = self._decrypt(token)
        token_type = self._token_type(payload)
        if token_type is TokenType.IDP_REFRESH_TOKEN and not allow_refresh:
            raise InvalidTokenError("refresh token not valid for this endpoint")
        user_id = self._user_id(payload)
        jti = self._jti(payload)
        iat = self._iat(payload)
        exp = self._exp(payload)

        user = await self._load_user(user_id)
        if user is None or not user.enabled:
            raise InvalidTokenError("user not found or disabled")

        if exp <= utc_now():
            raise InvalidTokenError("token expired")

        enabled = True
        if token_type is TokenType.IDP_REFRESH_TOKEN:
            enabled = await self._idp_refresh_token_live(self._row_id(payload))

        return AuthToken(
            id=jti,
            user_id=user_id,
            created_at=iat,
            updated_at=iat,
            enabled=enabled and user.enabled,
            expires_at=exp,
            token_type=token_type,
            scopes=_scopes_from_payload(payload),
        )

    async def _authenticate_api_key(self, raw_key: str) -> AuthToken:
        row = await self._load_api_key_row(hash_api_key_value(raw_key))
        if row is None:
            raise InvalidTokenError("unknown api key")
        now = utc_now()
        exp = row.expires_at if row.expires_at is not None else datetime.max.replace(tzinfo=UTC)
        if exp <= now:
            raise InvalidTokenError("api key expired")

        user = await self._load_user(row.creator_id)
        if user is None or not user.enabled:
            raise InvalidTokenError("user not found or disabled")

        return AuthToken(
            id=row.id,
            user_id=row.creator_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            enabled=row.enabled and user.enabled,
            expires_at=exp,
            token_type=TokenType.API_KEY,
        )

    # ------------------------------------------------------------------ #
    # Internals — minting.
    # ------------------------------------------------------------------ #

    def _mint_access_token(self, user_id: uuid.UUID, access_row: IdpAccessToken) -> str:
        ttl = self._ttl_until(access_row.expires_at)
        return self._enc.create_jwe_token(
            {
                _SUB_CLAIM: str(user_id),
                _TYP_CLAIM: TokenType.ACCESS_TOKEN.value,
                _JTI_CLAIM: str(uuid.uuid4()),
                _ACCESS_ID_CLAIM: str(access_row.id),
            },
            expires_in=ttl,
        )

    def _mint_cookie_token(self, user_id: uuid.UUID, access_row: IdpAccessToken) -> str:
        ttl = self._ttl_until(access_row.expires_at)
        return self._enc.create_jwe_token(
            {
                _SUB_CLAIM: str(user_id),
                _TYP_CLAIM: TokenType.COOKIE.value,
                _JTI_CLAIM: str(uuid.uuid4()),
                _ACCESS_ID_CLAIM: str(access_row.id),
                _ACCESS_EXP_CLAIM: int(access_row.expires_at.timestamp()),
            },
            expires_in=ttl,
        )

    def _mint_refresh_token(self, user_id: uuid.UUID, refresh_row: IdpRefreshToken) -> str:
        ttl = self._ttl_until(refresh_row.expires_at)
        return self._enc.create_jwe_token(
            {
                _SUB_CLAIM: str(user_id),
                _TYP_CLAIM: TokenType.IDP_REFRESH_TOKEN.value,
                _JTI_CLAIM: str(uuid.uuid4()),
                _ROW_ID_CLAIM: str(refresh_row.id),
            },
            expires_in=ttl,
        )

    @staticmethod
    def _ttl_until(expires_at: datetime) -> timedelta:
        ttl = _aware(expires_at) - utc_now()
        return ttl if ttl > _FLOOR_TTL else _FLOOR_TTL

    # ------------------------------------------------------------------ #
    # Internals — payload decode helpers.
    # ------------------------------------------------------------------ #

    def _decrypt(self, token: str) -> dict[str, object]:
        try:
            payload = self._enc.decrypt_jwe_token(token)
        except Exception as exc:
            raise InvalidTokenError("decryption failed") from exc
        if not isinstance(payload, dict):
            raise InvalidTokenError("invalid payload")
        return payload

    def _token_type(self, payload: dict[str, object]) -> TokenType:
        raw = payload.get(_TYP_CLAIM)
        if not isinstance(raw, str):
            raise InvalidTokenError("missing token type")
        try:
            return TokenType(raw)
        except ValueError as exc:
            raise InvalidTokenError("unknown token type") from exc

    def _user_id(self, payload: dict[str, object]) -> uuid.UUID:
        raw = payload.get(_SUB_CLAIM)
        if not isinstance(raw, str):
            raise InvalidTokenError("missing subject")
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise InvalidTokenError("invalid subject") from exc

    def _jti(self, payload: dict[str, object]) -> uuid.UUID:
        raw = payload.get(_JTI_CLAIM)
        if not isinstance(raw, str):
            raise InvalidTokenError("missing jti")
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise InvalidTokenError("invalid jti") from exc

    def _row_id(self, payload: dict[str, object]) -> uuid.UUID:
        raw = payload.get(_ROW_ID_CLAIM)
        if not isinstance(raw, str):
            raise InvalidTokenError("missing rid")
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise InvalidTokenError("invalid rid") from exc

    def _iat(self, payload: dict[str, object]) -> datetime:
        raw = payload.get(_IAT_CLAIM)
        if not isinstance(raw, int):
            raise InvalidTokenError("missing iat")
        return datetime.fromtimestamp(raw, tz=UTC)

    def _exp(self, payload: dict[str, object]) -> datetime:
        raw = payload.get(_EXP_CLAIM)
        if not isinstance(raw, int):
            return datetime.max.replace(tzinfo=UTC)
        return datetime.fromtimestamp(raw, tz=UTC)

    # ------------------------------------------------------------------ #
    # Internals — row loaders.
    # ------------------------------------------------------------------ #

    async def _load_user(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def _load_access_row(self, user_id: uuid.UUID) -> IdpAccessToken:
        result = await self._session.execute(
            select(IdpAccessToken)
            .join(IdpRefreshToken, IdpAccessToken.refresh_token_id == IdpRefreshToken.id)
            .where(IdpRefreshToken.creator_id == user_id)
            .order_by(IdpAccessToken.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise InvalidTokenError("no federated access token for user")
        return row

    async def _load_refresh_row_for_user(self, user_id: uuid.UUID) -> IdpRefreshToken:
        result = await self._session.execute(
            select(IdpRefreshToken)
            .where(IdpRefreshToken.creator_id == user_id)
            .order_by(IdpRefreshToken.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise InvalidTokenError("no federated refresh token for user")
        return row

    async def _load_api_key_row(self, key_hash: str) -> ApiKey | None:
        result = await self._session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
        return result.scalar_one_or_none()

    async def _idp_refresh_token_live(self, row_id: uuid.UUID) -> bool:
        row = await self._session.get(IdpRefreshToken, row_id)
        if row is None:
            return False
        return _aware(row.expires_at) > utc_now()
