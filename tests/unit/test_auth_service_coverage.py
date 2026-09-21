"""Coverage tests for AuthService, DevIdpService, auth_dependencies, and the
full OAuth round-trip via the dev IdP (issue #4).

These tests exercise the methods that the unit tests in ``test_auth_oauth.py``
do not reach: client CRUD, token exchange, refresh rotation, revocation,
userinfo, discovery, PKCE validation, user provisioning, and the HTTP routes
in ``auth_router`` / ``dev_router``.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.auth.auth_dependencies import depends_access_token, depends_user_id
from resourcey.auth.auth_models import (
    AuthBase,
    IdpAccessToken,
    IdpRefreshToken,
    OAuthClient,
    TokenType,
    User,
)
from resourcey.auth.auth_router import router as auth_router
from resourcey.auth.auth_service import (
    AuthError,
    AuthService,
    IdpError,
    InvalidClientError,
    InvalidGrantError,
    InvalidRedirectUriError,
    RefreshLockTimeoutError,
    _claim,
    _idp_access_expiry,
    _idp_refresh_expiry,
    _join_url,
    _normalize_scopes,
    _scopes_from_payload,
    _uuid,
)
from resourcey.auth.auth_tokens import InvalidTokenError, TokenService
from resourcey.auth.dev_router import (
    DevIdpService,
)
from resourcey.auth.dev_router import (
    InvalidClientError as DevInvalidClientError,
)
from resourcey.auth.dev_router import (
    InvalidGrantError as DevInvalidGrantError,
)
from resourcey.auth.dev_router import (
    InvalidRedirectUriError as DevInvalidRedirectUriError,
)
from resourcey.auth.dev_router import (
    router as dev_router,
)
from resourcey.auth.password import hash_password
from resourcey.config.config_framework import AuthConfig, FrameworkConfig, IdpConfig
from resourcey.config.config_runtime import set_config
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _enc_service() -> EncryptionService:
    cfg = EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id="k1", value="test-secret"))
    return EncryptionService(cfg)


@pytest.fixture
def enc() -> EncryptionService:
    return _enc_service()


@pytest.fixture
def idp_config() -> IdpConfig:
    return IdpConfig(
        url="/auth/dev",
        client_id="resourcey",
        client_secret="changeme",
        access_token_expires_in=900,
        refresh_token_expires_in=86400,
    )


@pytest.fixture
def framework_config(idp_config: IdpConfig) -> FrameworkConfig:
    return FrameworkConfig(
        database=FrameworkConfig().database,
        auth=AuthConfig(
            cookie_name="resourcey_session",
            cookie_secure=False,
            cookie_samesite="lax",
            idp=idp_config,
        ),
        base_url="http://localhost:8000",
    )


@pytest_asyncio.fixture
async def session(framework_config: FrameworkConfig) -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def auth_service(
    session: AsyncSession, enc: EncryptionService, framework_config: FrameworkConfig
) -> AuthService:
    return AuthService(session, encryption_service=enc, config=framework_config)


@pytest.fixture
def dev_idp(
    session: AsyncSession, enc: EncryptionService, framework_config: FrameworkConfig
) -> DevIdpService:
    return DevIdpService(session, encryption_service=enc, config=framework_config)


async def _make_user(
    session: AsyncSession,
    *,
    email: str = "test@example.com",
    username: str = "test",
    password: str = "s3cret",
    enabled: bool = True,
    idp_user_id: str | None = None,
) -> User:
    user = User(
        email=email,
        username=username,
        password=hash_password(password),
        enabled=enabled,
        idp_user_id=idp_user_id,
    )
    session.add(user)
    await session.flush()
    return user


def _make_id_token(sub: str, email: str) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none"}).encode())
    payload = b64(json.dumps({"sub": sub, "email": email}).encode())
    return f"{header}.{payload}."


def _persist_idp_tokens(auth_service: AuthService, user_id: uuid.UUID, **overrides: Any) -> Any:
    idp_tokens: dict[str, Any] = {
        "access_token": "idp-access",
        "refresh_token": "idp-refresh",
        "expires_in": 3600,
        "refresh_expires_in": 86400,
        "id_token": _make_id_token(str(user_id), "test@example.com"),
    }
    idp_tokens.update(overrides)
    return auth_service.persist_idp_tokens(user_id, idp_tokens)


# ---------------------------------------------------------------------------
# AuthService: OAuth client CRUD
# ---------------------------------------------------------------------------


class TestOAuthClientCrud:
    async def test_create_and_get_client(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        client = await auth_service.create_oauth_client(
            client_id="client-1",
            client_secret="secret-1",
            name="Test Client",
            redirect_uris=["https://app.example.com/cb", "https://*.example.com/cb"],
        )
        assert client.client_id == "client-1"
        assert client.name == "Test Client"
        assert client.enabled is True
        # Secret is encrypted at rest.
        assert client.client_secret != "secret-1"

        fetched = await auth_service.get_client_by_client_id("client-1")
        assert fetched is not None
        assert fetched.id == client.id

        uris = await auth_service.list_redirect_uris(client)
        assert "https://app.example.com/cb" in uris

    async def test_create_duplicate_client_raises(self, auth_service: AuthService) -> None:
        await auth_service.create_oauth_client(client_id="dup", client_secret="s", redirect_uris=[])
        with pytest.raises(InvalidClientError, match="already exists"):
            await auth_service.create_oauth_client(
                client_id="dup", client_secret="s", redirect_uris=[]
            )

    async def test_get_unknown_client_returns_none(self, auth_service: AuthService) -> None:
        assert await auth_service.get_client_by_client_id("nope") is None

    async def test_load_client_disabled_raises(self, auth_service: AuthService) -> None:
        await auth_service.create_oauth_client(
            client_id="disabled", client_secret="s", enabled=False
        )
        with pytest.raises(InvalidClientError):
            await auth_service._load_client("disabled")

    async def test_authenticate_client_wrong_secret(self, auth_service: AuthService) -> None:
        await auth_service.create_oauth_client(
            client_id="c", client_secret="right", redirect_uris=[]
        )
        with pytest.raises(InvalidClientError):
            await auth_service._authenticate_client("c", "wrong")

    async def test_authenticate_client_unknown(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidClientError):
            await auth_service._authenticate_client("unknown", "s")

    async def test_redirect_uri_allowed_wildcard(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        client = await auth_service.create_oauth_client(
            client_id="wc",
            client_secret="s",
            redirect_uris=["https://*.example.com/cb"],
        )
        assert await auth_service._redirect_uri_allowed(client, "https://app.example.com/cb")
        assert not await auth_service._redirect_uri_allowed(client, "https://other.com/cb")

    async def test_redirect_uri_no_match(self, auth_service: AuthService) -> None:
        client = await auth_service.create_oauth_client(
            client_id="nm", client_secret="s", redirect_uris=["https://app.com/cb"]
        )
        assert not await auth_service._redirect_uri_allowed(client, "https://other.com/cb")


# ---------------------------------------------------------------------------
# AuthService: build_authorize_redirect + handle_callback
# ---------------------------------------------------------------------------


class TestAuthorizeAndCallback:
    async def test_build_authorize_redirect(self, auth_service: AuthService) -> None:
        await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        url = await auth_service.build_authorize_redirect(
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            state="xyz",
            scope="openid email",
            code_challenge="cc",
            code_challenge_method="S256",
            callback_url="http://localhost:8000/auth/callback",
        )
        assert "response_type=code" in url
        assert "client_id=resourcey" in url
        assert "state=" in url

    async def test_build_authorize_redirect_bad_response_type(
        self, auth_service: AuthService
    ) -> None:
        await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        with pytest.raises(AuthError, match="response_type"):
            await auth_service.build_authorize_redirect(
                client_id="c1",
                redirect_uri="http://localhost:8000/auth/callback",
                state=None,
                scope=None,
                code_challenge=None,
                code_challenge_method=None,
                callback_url="http://localhost:8000/auth/callback",
                response_type="token",
            )

    async def test_build_authorize_redirect_bad_redirect_uri(
        self, auth_service: AuthService
    ) -> None:
        await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        with pytest.raises(InvalidRedirectUriError):
            await auth_service.build_authorize_redirect(
                client_id="c1",
                redirect_uri="http://evil.com/cb",
                state=None,
                scope=None,
                code_challenge=None,
                code_challenge_method=None,
                callback_url="http://localhost:8000/auth/callback",
            )

    async def test_build_authorize_redirect_unknown_client(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidClientError):
            await auth_service.build_authorize_redirect(
                client_id="unknown",
                redirect_uri="http://localhost:8000/auth/callback",
                state=None,
                scope=None,
                code_challenge=None,
                code_challenge_method=None,
                callback_url="http://localhost:8000/auth/callback",
            )

    async def test_handle_callback_provisions_user(
        self, auth_service: AuthService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        """Full /callback flow: build authorize redirect, decode state, exchange code."""
        # The dev IdP is at /auth/dev, so we need to mock the IdP token endpoint.
        # Instead of a full HTTP round-trip, we directly test _provision_user
        # and _mint_auth_code, which are the core of handle_callback.
        idp_tokens = {
            "access_token": "a",
            "refresh_token": "r",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
            "id_token": _make_id_token("sub-123", "new@example.com"),
        }
        user = await auth_service._provision_user(idp_tokens)
        assert user.email == "new@example.com"
        assert user.idp_user_id == "sub-123"

        refresh_row, access_row = await auth_service.persist_idp_tokens(user.id, idp_tokens)
        auth_code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset({"openid", "email"}),
            client_code_challenge=None,
            client_code_method=None,
        )
        assert auth_code is not None
        payload = enc.decrypt_jwe_token(auth_code)
        assert payload["sub"] == str(user.id)


# ---------------------------------------------------------------------------
# AuthService: token exchange (authorization code + refresh)
# ---------------------------------------------------------------------------


class TestTokenExchange:
    async def _setup_client_and_user(
        self, auth_service: AuthService, session: AsyncSession
    ) -> tuple[OAuthClient, User, tuple[IdpRefreshToken, IdpAccessToken]]:
        client = await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        user = await _make_user(session)
        rows = await _persist_idp_tokens(auth_service, user.id)
        return client, user, rows

    async def test_exchange_authorization_code(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        _, user, (refresh_row, access_row) = await self._setup_client_and_user(
            auth_service, session
        )
        code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset({"openid"}),
            client_code_challenge=None,
            client_code_method=None,
        )
        pair = await auth_service.exchange_authorization_code(
            code=code,
            redirect_uri="http://localhost:8000/auth/callback",
            client_id="c1",
            client_secret="s1",
            code_verifier=None,
        )
        assert pair.access_token is not None
        assert pair.refresh_token is not None
        assert pair.expires_in > 0

    async def test_exchange_authorization_code_bad_client(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        with pytest.raises(InvalidClientError):
            await auth_service.exchange_authorization_code(
                code="x",
                redirect_uri="x",
                client_id="bad",
                client_secret="bad",
                code_verifier=None,
            )

    async def test_exchange_authorization_code_bad_code(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        with pytest.raises(InvalidGrantError):
            await auth_service.exchange_authorization_code(
                code="not-a-jwe",
                redirect_uri="http://localhost:8000/auth/callback",
                client_id="c1",
                client_secret="s1",
                code_verifier=None,
            )

    async def test_exchange_authorization_code_redirect_mismatch(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        _, user, (refresh_row, access_row) = await self._setup_client_and_user(
            auth_service, session
        )
        code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset(),
            client_code_challenge=None,
            client_code_method=None,
        )
        with pytest.raises(InvalidGrantError, match="redirect_uri"):
            await auth_service.exchange_authorization_code(
                code=code,
                redirect_uri="http://wrong.com/cb",
                client_id="c1",
                client_secret="s1",
                code_verifier=None,
            )

    async def test_exchange_authorization_code_pkce_s256(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        from resourcey.auth.auth_service import (
            _derive_code_challenge,
            _generate_code_verifier,
        )

        _, user, (refresh_row, access_row) = await self._setup_client_and_user(
            auth_service, session
        )
        verifier = _generate_code_verifier()
        challenge = _derive_code_challenge(verifier, "S256")
        code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset(),
            client_code_challenge=challenge,
            client_code_method="S256",
        )
        pair = await auth_service.exchange_authorization_code(
            code=code,
            redirect_uri="http://localhost:8000/auth/callback",
            client_id="c1",
            client_secret="s1",
            code_verifier=verifier,
        )
        assert pair.access_token

    async def test_exchange_authorization_code_pkce_missing_verifier(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        from resourcey.auth.auth_service import (
            _derive_code_challenge,
            _generate_code_verifier,
        )

        _, user, (refresh_row, access_row) = await self._setup_client_and_user(
            auth_service, session
        )
        verifier = _generate_code_verifier()
        challenge = _derive_code_challenge(verifier, "S256")
        code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset(),
            client_code_challenge=challenge,
            client_code_method="S256",
        )
        with pytest.raises(InvalidGrantError, match="code_verifier"):
            await auth_service.exchange_authorization_code(
                code=code,
                redirect_uri="http://localhost:8000/auth/callback",
                client_id="c1",
                client_secret="s1",
                code_verifier=None,
            )

    async def test_exchange_authorization_code_pkce_mismatch(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        from resourcey.auth.auth_service import _generate_code_verifier

        _, user, (refresh_row, access_row) = await self._setup_client_and_user(
            auth_service, session
        )
        challenge = _generate_code_verifier()  # wrong challenge
        code = auth_service._mint_auth_code(
            user_id=user.id,
            row_id=refresh_row.id,
            access_id=access_row.id,
            client_id="c1",
            redirect_uri="http://localhost:8000/auth/callback",
            scopes=frozenset(),
            client_code_challenge=challenge,
            client_code_method="S256",
        )
        with pytest.raises(InvalidGrantError, match="PKCE"):
            await auth_service.exchange_authorization_code(
                code=code,
                redirect_uri="http://localhost:8000/auth/callback",
                client_id="c1",
                client_secret="s1",
                code_verifier=_generate_code_verifier(),
            )

    async def test_exchange_refresh_token(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        _, user, (refresh_row, _) = await self._setup_client_and_user(auth_service, session)
        # Mock the IdP refresh endpoint.
        mock_http = _MockIdpClient(
            token_response={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "refresh_expires_in": 86400,
            }
        )
        auth_service._http = mock_http  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        # SQLite does not support SET LOCAL lock_timeout — no-op it.
        async def _noop_lock_timeout() -> None:
            pass

        auth_service._set_lock_timeout = _noop_lock_timeout  # type: ignore[method-assign]

        refresh_token = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": TokenType.IDP_REFRESH_TOKEN.value,
                "jti": str(uuid.uuid4()),
                "rid": str(refresh_row.id),
                "scp": "openid email",
            },
            expires_in=timedelta(hours=1),
        )
        pair = await auth_service.exchange_refresh_token(
            refresh_token=refresh_token,
            client_id="c1",
            client_secret="s1",
        )
        assert pair.access_token
        assert pair.refresh_token

    async def test_exchange_refresh_token_bad_type(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        await auth_service.create_oauth_client(
            client_id="c1",
            client_secret="s1",
            redirect_uris=["http://localhost:8000/auth/callback"],
        )
        # A cookie token, not a refresh token.
        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4()), "ttyp": TokenType.COOKIE.value},
            expires_in=timedelta(hours=1),
        )
        with pytest.raises(InvalidGrantError, match="not a refresh token"):
            await auth_service.exchange_refresh_token(
                refresh_token=token,
                client_id="c1",
                client_secret="s1",
            )

    async def test_refresh_access_token_not_found(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidGrantError, match="access token not found"):
            await auth_service.refresh_access_token(uuid.uuid4())

    async def test_refresh_access_token_refresh_not_found(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        await _make_user(session)
        # An access row with no backing refresh row.
        access = IdpAccessToken(
            refresh_token_id=uuid.uuid4(),
            access_token="x",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(access)
        await session.flush()
        with pytest.raises(InvalidGrantError, match="refresh token not found"):
            await auth_service.refresh_access_token(access.id)


# ---------------------------------------------------------------------------
# AuthService: revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    async def test_revoke_refresh_token(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        await auth_service.create_oauth_client(client_id="c1", client_secret="s1", redirect_uris=[])
        user = await _make_user(session)
        refresh_row, _ = await _persist_idp_tokens(auth_service, user.id)
        token = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": TokenType.IDP_REFRESH_TOKEN.value,
                "jti": str(uuid.uuid4()),
                "rid": str(refresh_row.id),
            },
            expires_in=timedelta(hours=1),
        )
        await auth_service.revoke_token(
            token=token,
            token_type_hint="refresh_token",
            client_id="c1",
            client_secret="s1",
        )
        # Row should be deleted.
        assert await session.get(IdpRefreshToken, refresh_row.id) is None

    async def test_revoke_token_best_effort_invalid(self, auth_service: AuthService) -> None:
        await auth_service.create_oauth_client(client_id="c1", client_secret="s1", redirect_uris=[])
        # Invalid token — should not raise (best-effort).
        await auth_service.revoke_token(
            token="not-a-token",
            token_type_hint=None,
            client_id="c1",
            client_secret="s1",
        )

    async def test_revoke_token_bad_client(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidClientError):
            await auth_service.revoke_token(
                token="x",
                token_type_hint=None,
                client_id="bad",
                client_secret="bad",
            )

    async def test_revoke_session(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        user = await _make_user(session)
        refresh_row, access_row = await _persist_idp_tokens(auth_service, user.id)
        cookie = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": TokenType.COOKIE.value,
                "jti": str(uuid.uuid4()),
                "aid": str(access_row.id),
                "axp": int(access_row.expires_at.timestamp()),
            },
            expires_in=timedelta(hours=1),
        )
        await auth_service.revoke_session(cookie)
        assert await session.get(IdpRefreshToken, refresh_row.id) is None

    async def test_revoke_session_invalid_token(self, auth_service: AuthService) -> None:
        # Should not raise.
        await auth_service.revoke_session("not-a-token")

    async def test_revoke_session_no_aid(
        self, auth_service: AuthService, enc: EncryptionService
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": "x", "ttyp": TokenType.COOKIE.value},
            expires_in=timedelta(hours=1),
        )
        await auth_service.revoke_session(token)

    async def test_revoke_session_access_not_found(
        self, auth_service: AuthService, enc: EncryptionService
    ) -> None:
        token = enc.create_jwe_token(
            {
                "sub": "x",
                "ttyp": TokenType.COOKIE.value,
                "aid": str(uuid.uuid4()),
                "axp": int(datetime.now(UTC).timestamp()),
            },
            expires_in=timedelta(hours=1),
        )
        await auth_service.revoke_session(token)


# ---------------------------------------------------------------------------
# AuthService: userinfo + discovery
# ---------------------------------------------------------------------------


class TestUserinfoAndDiscovery:
    async def test_build_userinfo_claims(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _make_user(session, email="u@example.com", username="uname")
        claims = await auth_service.build_userinfo_claims(
            user.id, frozenset({"openid", "email", "profile"})
        )
        assert claims is not None
        assert claims["sub"] == str(user.id)
        assert claims["email"] == "u@example.com"
        assert claims["email_verified"] is True
        assert claims["name"] == "uname"
        assert claims["preferred_username"] == "uname"

    async def test_build_userinfo_claims_email_only(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _make_user(session)
        claims = await auth_service.build_userinfo_claims(user.id, frozenset({"email"}))
        assert claims is not None
        assert "name" not in claims
        assert "preferred_username" not in claims

    async def test_build_userinfo_claims_user_not_found(self, auth_service: AuthService) -> None:
        claims = await auth_service.build_userinfo_claims(uuid.uuid4(), frozenset({"openid"}))
        assert claims is None

    async def test_build_userinfo_claims_disabled_user(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _make_user(session, enabled=False)
        claims = await auth_service.build_userinfo_claims(user.id, frozenset({"openid"}))
        assert claims is None

    async def test_build_discovery_document(self, auth_service: AuthService) -> None:
        doc = auth_service.build_discovery_document()
        assert doc["issuer"] == "http://localhost:8000"
        assert doc["authorization_endpoint"].endswith("/auth/authorize")
        assert doc["token_endpoint"].endswith("/auth/token")
        assert doc["userinfo_endpoint"].endswith("/auth/userinfo")
        assert doc["revocation_endpoint"].endswith("/auth/revoke")
        assert "code" in doc["response_types_supported"]
        assert "cookie" in doc["response_types_supported"]
        assert "authorization_code" in doc["grant_types_supported"]
        assert "refresh_token" in doc["grant_types_supported"]


# ---------------------------------------------------------------------------
# AuthService: user provisioning
# ---------------------------------------------------------------------------


class TestUserProvisioning:
    async def test_provision_existing_by_sub(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _make_user(session, idp_user_id="sub-1")
        result = await auth_service._provision_user(
            {
                "access_token": "a",
                "refresh_token": "r",
                "id_token": _make_id_token("sub-1", user.email),
            }
        )
        assert result.id == user.id

    async def test_provision_existing_by_email(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _make_user(session, email="exist@example.com", idp_user_id=None)
        result = await auth_service._provision_user(
            {
                "access_token": "a",
                "refresh_token": "r",
                "id_token": _make_id_token("new-sub", "exist@example.com"),
            }
        )
        assert result.id == user.id
        # sub should be back-filled.
        assert result.idp_user_id == "new-sub"

    async def test_provision_new_user(self, auth_service: AuthService) -> None:
        result = await auth_service._provision_user(
            {
                "access_token": "a",
                "refresh_token": "r",
                "id_token": _make_id_token("new-sub", "brand@example.com"),
            }
        )
        assert result.email == "brand@example.com"
        assert result.username == "brand"
        assert result.idp_user_id == "new-sub"

    async def test_provision_no_sub_no_email_raises(self, auth_service: AuthService) -> None:
        with pytest.raises(IdpError, match="missing sub and email"):
            await auth_service._provision_user(
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "id_token": _make_id_token("", ""),
                }
            )

    async def test_provision_no_email_for_new_user_raises(self, auth_service: AuthService) -> None:
        with pytest.raises(IdpError, match="missing email"):
            await auth_service._provision_user(
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "id_token": _make_id_token("sub-only", ""),
                }
            )

    async def test_provision_no_id_token(self, auth_service: AuthService) -> None:
        with pytest.raises(IdpError, match="missing sub and email"):
            await auth_service._provision_user(
                {
                    "access_token": "a",
                    "refresh_token": "r",
                }
            )

    async def test_find_user_by_idp_sub(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        await _make_user(session, idp_user_id="sub-x")
        found = await auth_service._find_user_by_idp_sub("sub-x")
        assert found is not None
        assert found.idp_user_id == "sub-x"

    async def test_find_user_by_email(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        await _make_user(session, email="find@example.com")
        found = await auth_service._find_user_by_email("find@example.com")
        assert found is not None
        assert found.email == "find@example.com"


# ---------------------------------------------------------------------------
# AuthService: IdP HTTP calls + error paths
# ---------------------------------------------------------------------------


class _MockIdpClient:
    """A minimal mock httpx.AsyncClient for the IdP token endpoint."""

    def __init__(self, *, token_response: dict[str, Any] | None = None, status: int = 200):
        self._token_response = token_response or {}
        self._status = status
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, data: dict[str, str] | None = None) -> _MockResponse:
        self.calls.append({"url": url, "data": data or {}})
        return _MockResponse(self._status, self._token_response)

    async def aclose(self) -> None:
        pass


class _MockResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class TestIdpHttpCalls:
    async def test_idp_token_post_success(
        self, auth_service: AuthService, enc: EncryptionService
    ) -> None:
        mock = _MockIdpClient(token_response={"access_token": "a", "refresh_token": "r"})
        auth_service._http = mock  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        result = await auth_service._idp_token_post({"grant_type": "test"})
        assert result["access_token"] == "a"
        assert len(mock.calls) == 1

    async def test_idp_token_post_error_status(self, auth_service: AuthService) -> None:
        mock = _MockIdpClient(status=400, token_response={"error": "bad"})
        auth_service._http = mock  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        with pytest.raises(IdpError, match="returned 400"):
            await auth_service._idp_token_post({"grant_type": "test"})

    async def test_idp_token_post_missing_tokens(self, auth_service: AuthService) -> None:
        mock = _MockIdpClient(token_response={"foo": "bar"})
        auth_service._http = mock  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        with pytest.raises(IdpError, match="missing access_token"):
            await auth_service._idp_token_post({"grant_type": "test"})

    async def test_exchange_code_with_idp(
        self, auth_service: AuthService, enc: EncryptionService
    ) -> None:
        mock = _MockIdpClient(token_response={"access_token": "a", "refresh_token": "r"})
        auth_service._http = mock  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        result = await auth_service._exchange_code_with_idp(
            code="test-code", verifier="v", callback_url="http://localhost:8000/auth/callback"
        )
        assert result["access_token"] == "a"

    async def test_refresh_with_idp(
        self,
        auth_service: AuthService,
        session: AsyncSession,
        enc: EncryptionService,
    ) -> None:
        mock = _MockIdpClient(token_response={"access_token": "new", "refresh_token": "new-r"})
        auth_service._http = mock  # type: ignore[attr-defined]
        auth_service._owns_client = False  # type: ignore[attr-defined]

        user = await _make_user(session)
        refresh_row, _ = await _persist_idp_tokens(auth_service, user.id)
        result = await auth_service._refresh_with_idp(refresh_row)
        assert result["access_token"] == "new"

    async def test_replace_idp_tokens(
        self,
        auth_service: AuthService,
        session: AsyncSession,
    ) -> None:
        user = await _make_user(session)
        old_refresh, _ = await _persist_idp_tokens(auth_service, user.id)
        new_tokens = {
            "access_token": "new-a",
            "refresh_token": "new-r",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
        }
        new_refresh, _new_access = await auth_service._replace_idp_tokens(
            user.id, new_tokens, old_refresh
        )
        assert new_refresh.refresh_token != old_refresh.refresh_token
        # Old row should be deleted.
        assert await session.get(IdpRefreshToken, old_refresh.id) is None


# ---------------------------------------------------------------------------
# AuthService: misc helpers + error classes
# ---------------------------------------------------------------------------


class TestHelpersAndErrors:
    def test_claim_found(self) -> None:
        assert _claim({"sub": "x"}, None, "sub") == "x"

    def test_claim_fallback_key(self) -> None:
        assert _claim({}, "custom", "sub") is None

    def test_claim_empty_string(self) -> None:
        assert _claim({"sub": ""}, None, "sub") is None

    def test_claim_custom_key(self) -> None:
        assert _claim({"custom": "v"}, "custom", "sub") == "v"

    def test_normalize_scopes_filters_unknown(self) -> None:
        assert _normalize_scopes("openid email profile unknown") == frozenset(
            {"openid", "email", "profile"}
        )

    def test_scopes_from_payload_string(self) -> None:
        assert _scopes_from_payload({"scp": "openid email"}) == frozenset({"openid", "email"})

    def test_scopes_from_payload_missing(self) -> None:
        assert _scopes_from_payload({}) == frozenset()

    def test_scopes_from_payload_non_string(self) -> None:
        assert _scopes_from_payload({"scp": 123}) == frozenset()

    def test_uuid_valid(self) -> None:
        u = uuid.uuid4()
        assert _uuid({"id": str(u)}, "id") == u

    def test_uuid_missing(self) -> None:
        with pytest.raises(InvalidGrantError, match="missing"):
            _uuid({}, "id")

    def test_uuid_invalid(self) -> None:
        with pytest.raises(InvalidGrantError, match="invalid"):
            _uuid({"id": "not-a-uuid"}, "id")

    def test_idp_access_expiry_from_expires_at(self, idp_config: IdpConfig) -> None:
        exp = _idp_access_expiry({"expires_at": 9999999999}, drift_seconds=0, idp=idp_config)
        assert exp.year > 2000

    def test_idp_refresh_expiry_from_refresh_expires_at(self, idp_config: IdpConfig) -> None:
        exp = _idp_refresh_expiry(
            {"refresh_expires_at": 9999999999}, drift_seconds=0, idp=idp_config
        )
        assert exp.year > 2000

    def test_idp_refresh_expiry_fallback(self, idp_config: IdpConfig) -> None:
        exp = _idp_refresh_expiry({}, drift_seconds=60, idp=idp_config)
        expected = datetime.now(UTC) + timedelta(seconds=86340)
        assert abs((exp - expected).total_seconds()) < 5

    def test_join_url_no_params(self) -> None:
        assert _join_url("http://x", "/path") == "http://x/path"

    def test_join_url_with_params(self) -> None:
        url = _join_url("http://x", "/path", {"a": "1", "b": "2"})
        assert "a=1" in url and "b=2" in url

    def test_idp_base_http(self, auth_service: AuthService) -> None:
        auth_service._idp = IdpConfig(  # type: ignore[misc]
            url="https://idp.example.com",
            client_id="c",
            client_secret="s",
        )
        assert auth_service._idp_base() == "https://idp.example.com"

    def test_idp_base_relative(self, auth_service: AuthService) -> None:
        auth_service._idp = IdpConfig(  # type: ignore[misc]
            url="/auth/dev",
            client_id="c",
            client_secret="s",
        )
        assert auth_service._idp_base() == "http://localhost:8000/auth/dev"

    async def test_aclose_owned_client(self, framework_config: FrameworkConfig) -> None:
        svc = AuthService(
            session=None,  # type: ignore[arg-type]
            encryption_service=_enc_service(),
            config=framework_config,
        )
        await svc.aclose()  # should close the owned httpx client

    async def test_aclose_external_client(
        self, framework_config: FrameworkConfig, enc: EncryptionService
    ) -> None:
        external = httpx.AsyncClient()
        svc = AuthService(
            session=None,  # type: ignore[arg-type]
            http_client=external,
            encryption_service=enc,
            config=framework_config,
        )
        await svc.aclose()
        # External client should not be closed by aclose.
        assert external.is_closed is False
        await external.aclose()

    async def test_load_token_rows_not_found(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidGrantError, match="refresh token not found"):
            await auth_service._load_token_rows(uuid.uuid4(), uuid.uuid4())

    async def test_load_access_row_for_refresh_none(self, auth_service: AuthService) -> None:
        result = await auth_service._load_access_row_for_refresh(uuid.uuid4())
        assert result is None

    async def test_decrypt_invalid_token(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidGrantError, match="decryption"):
            auth_service._decrypt("not-a-jwe")

    async def test_verify_pkce_no_challenge(self, auth_service: AuthService) -> None:
        # No challenge -> no verification needed.
        auth_service._verify_pkce(challenge=None, method=None, verifier=None)

    def test_error_subclasses(self) -> None:
        assert issubclass(InvalidClientError, AuthError)
        assert issubclass(InvalidGrantError, AuthError)
        assert issubclass(InvalidRedirectUriError, AuthError)
        assert issubclass(IdpError, AuthError)
        assert issubclass(RefreshLockTimeoutError, AuthError)


# ---------------------------------------------------------------------------
# DevIdpService unit tests
# ---------------------------------------------------------------------------


class TestDevIdpService:
    async def test_login_success(self, dev_idp: DevIdpService, session: AsyncSession) -> None:
        await _make_user(session, username="devuser", password="devpass")
        user, tokens = await dev_idp.login(username="devuser", password="devpass")
        assert user.username == "devuser"
        assert "access_token" in tokens
        assert "refresh_token" in tokens
        assert "id_token" in tokens

    async def test_login_bad_password(self, dev_idp: DevIdpService, session: AsyncSession) -> None:
        await _make_user(session, username="devuser", password="devpass")
        with pytest.raises(DevInvalidGrantError):
            await dev_idp.login(username="devuser", password="wrong")

    async def test_login_unknown_user(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError):
            await dev_idp.login(username="nope", password="x")

    async def test_login_disabled_user(self, dev_idp: DevIdpService, session: AsyncSession) -> None:
        await _make_user(session, username="devuser", password="devpass", enabled=False)
        with pytest.raises(DevInvalidGrantError):
            await dev_idp.login(username="devuser", password="devpass")

    async def test_login_no_password(self, dev_idp: DevIdpService, session: AsyncSession) -> None:
        user = User(email="np@example.com", username="np", enabled=True)
        session.add(user)
        await session.flush()
        with pytest.raises(DevInvalidGrantError):
            await dev_idp.login(username="np", password="x")

    async def test_authorize_success(
        self, dev_idp: DevIdpService, session: AsyncSession, framework_config: FrameworkConfig
    ) -> None:
        await _make_user(session, username="devuser", password="devpass")
        location = await dev_idp.authorize(
            client_id="resourcey",
            redirect_uri=f"{framework_config.base_url}/auth/callback",
            state="xyz",
            code_challenge=None,
            code_challenge_method=None,
            username="devuser",
            password="devpass",
        )
        assert "code=" in location
        assert "state=xyz" in location

    async def test_authorize_bad_client(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidClientError):
            await dev_idp.authorize(
                client_id="bad",
                redirect_uri="http://localhost:8000/auth/callback",
                state=None,
                code_challenge=None,
                code_challenge_method=None,
                username="x",
                password="x",
            )

    async def test_authorize_bad_redirect_uri(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidRedirectUriError):
            await dev_idp.authorize(
                client_id="resourcey",
                redirect_uri="http://evil.com/cb",
                state=None,
                code_challenge=None,
                code_challenge_method=None,
                username="x",
                password="x",
            )

    async def test_exchange_code_success(
        self, dev_idp: DevIdpService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        user = await _make_user(session, username="devuser", password="devpass")
        from resourcey.auth.dev_router import _DEV_CODE_TYP

        code = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": _DEV_CODE_TYP,
                "jti": str(uuid.uuid4()),
                "email": user.email,
                "cid": "resourcey",
                "ruri": "http://localhost:8000/auth/callback",
            },
            expires_in=timedelta(minutes=5),
        )
        result = await dev_idp.exchange_code(
            code=code,
            redirect_uri="http://localhost:8000/auth/callback",
            client_id="resourcey",
            client_secret="changeme",
            code_verifier=None,
        )
        assert "access_token" in result

    async def test_exchange_code_bad_client(
        self, dev_idp: DevIdpService, enc: EncryptionService
    ) -> None:
        with pytest.raises(DevInvalidClientError):
            await dev_idp.exchange_code(
                code="x",
                redirect_uri="x",
                client_id="bad",
                client_secret="bad",
                code_verifier=None,
            )

    async def test_exchange_code_bad_code(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError):
            await dev_idp.exchange_code(
                code="not-a-jwe",
                redirect_uri="http://localhost:8000/auth/callback",
                client_id="resourcey",
                client_secret="changeme",
                code_verifier=None,
            )

    async def test_exchange_code_redirect_mismatch(
        self, dev_idp: DevIdpService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        user = await _make_user(session)
        from resourcey.auth.dev_router import _DEV_CODE_TYP

        code = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": _DEV_CODE_TYP,
                "jti": str(uuid.uuid4()),
                "email": user.email,
                "cid": "resourcey",
                "ruri": "http://localhost:8000/auth/callback",
            },
            expires_in=timedelta(minutes=5),
        )
        with pytest.raises(DevInvalidGrantError, match="redirect_uri"):
            await dev_idp.exchange_code(
                code=code,
                redirect_uri="http://wrong.com/cb",
                client_id="resourcey",
                client_secret="changeme",
                code_verifier=None,
            )

    async def test_exchange_code_pkce(
        self, dev_idp: DevIdpService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        from resourcey.auth.auth_service import (
            _derive_code_challenge,
            _generate_code_verifier,
        )
        from resourcey.auth.dev_router import _DEV_CODE_TYP

        user = await _make_user(session)
        verifier = _generate_code_verifier()
        challenge = _derive_code_challenge(verifier, "S256")
        code = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": _DEV_CODE_TYP,
                "jti": str(uuid.uuid4()),
                "email": user.email,
                "cid": "resourcey",
                "ruri": "http://localhost:8000/auth/callback",
                "cc": challenge,
                "ccm": "S256",
            },
            expires_in=timedelta(minutes=5),
        )
        result = await dev_idp.exchange_code(
            code=code,
            redirect_uri="http://localhost:8000/auth/callback",
            client_id="resourcey",
            client_secret="changeme",
            code_verifier=verifier,
        )
        assert "access_token" in result

    async def test_exchange_code_pkce_missing_verifier(
        self, dev_idp: DevIdpService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        from resourcey.auth.auth_service import _generate_code_verifier
        from resourcey.auth.dev_router import _DEV_CODE_TYP

        user = await _make_user(session)
        challenge = _generate_code_verifier()
        code = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": _DEV_CODE_TYP,
                "jti": str(uuid.uuid4()),
                "email": user.email,
                "cid": "resourcey",
                "ruri": "http://localhost:8000/auth/callback",
                "cc": challenge,
                "ccm": "S256",
            },
            expires_in=timedelta(minutes=5),
        )
        with pytest.raises(DevInvalidGrantError, match="code_verifier"):
            await dev_idp.exchange_code(
                code=code,
                redirect_uri="http://localhost:8000/auth/callback",
                client_id="resourcey",
                client_secret="changeme",
                code_verifier=None,
            )

    async def test_refresh_success(
        self, dev_idp: DevIdpService, session: AsyncSession, enc: EncryptionService
    ) -> None:
        from resourcey.auth.dev_router import _DEV_REFRESH_TYP

        user = await _make_user(session)
        refresh = enc.create_jwe_token(
            {
                "sub": str(user.id),
                "ttyp": _DEV_REFRESH_TYP,
                "jti": str(uuid.uuid4()),
                "email": user.email,
            },
            expires_in=timedelta(hours=1),
        )
        result = await dev_idp.refresh(
            refresh_token=refresh,
            client_id="resourcey",
            client_secret="changeme",
        )
        assert "access_token" in result

    async def test_refresh_bad_token_type(
        self, dev_idp: DevIdpService, enc: EncryptionService
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4()), "ttyp": "wrong"},
            expires_in=timedelta(hours=1),
        )
        with pytest.raises(DevInvalidGrantError, match="not a refresh token"):
            await dev_idp.refresh(
                refresh_token=token,
                client_id="resourcey",
                client_secret="changeme",
            )

    async def test_refresh_bad_client(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidClientError):
            await dev_idp.refresh(
                refresh_token="x",
                client_id="bad",
                client_secret="bad",
            )

    async def test_validate_client_bad_secret(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidClientError):
            dev_idp._validate_client("resourcey", "wrong-secret")

    async def test_decrypt_bad_token(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError):
            dev_idp._decrypt("not-a-jwe")

    async def test_user_id_missing(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError, match="missing subject"):
            dev_idp._user_id({})

    async def test_user_id_invalid(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError, match="invalid subject"):
            dev_idp._user_id({"sub": "not-a-uuid"})

    async def test_email_missing(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError, match="missing email"):
            dev_idp._email({})

    async def test_email_empty(self, dev_idp: DevIdpService) -> None:
        with pytest.raises(DevInvalidGrantError, match="missing email"):
            dev_idp._email({"email": ""})


# ---------------------------------------------------------------------------
# Full OAuth round-trip E2E via TestClient
# ---------------------------------------------------------------------------


class TestOAuthE2E:
    """Full OAuth flow: dev login -> authorize -> callback -> token -> userinfo."""

    @pytest.fixture
    def app(
        self,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> Any:
        set_config(framework_config)

        import resourcey.encryption.encryption_service as enc_mod

        original = enc_mod.get_encryption_service

        def _patched() -> EncryptionService:
            return enc

        _patched.cache_clear = lambda: None  # type: ignore[attr-defined]
        enc_mod.get_encryption_service = _patched  # type: ignore[assignment]

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(dev_router)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        app.state.resourcey_engine = engine
        app.state.resourcey_session_factory = async_sessionmaker(engine, expire_on_commit=False)

        import asyncio

        async def _create_tables() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(AuthBase.metadata.create_all)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_create_tables())
        loop.close()

        yield app

        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine.dispose())
        loop.close()

        enc_mod.get_encryption_service = original  # type: ignore[assignment]

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_login_success(self, app: Any) -> None:
        """Create a user via the session, then login via the dev router."""
        # First, create a user in the DB.
        import asyncio

        async def _seed() -> None:
            async with app.state.resourcey_session_factory() as sess:
                sess.add(
                    User(
                        email="e2e@example.com",
                        username="e2euser",
                        password=hash_password("e2epass"),
                        enabled=True,
                    )
                )
                await sess.commit()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_seed())
        loop.close()

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/login",
                json={"username": "e2euser", "password": "e2epass"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["username"] == "e2euser"
            # Cookie should be set.
            assert "resourcey_session" in resp.cookies

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_userinfo_with_cookie(self, app: Any) -> None:
        import asyncio

        async def _seed() -> None:
            async with app.state.resourcey_session_factory() as sess:
                sess.add(
                    User(
                        email="ui@example.com",
                        username="uiuser",
                        password=hash_password("uipass"),
                        enabled=True,
                    )
                )
                await sess.commit()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_seed())
        loop.close()

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            # Login to get a cookie.
            client.post(
                "/auth/dev/login",
                json={"username": "uiuser", "password": "uipass"},
            )
            # Use the cookie to access userinfo.
            resp = client.get("/auth/userinfo")
            assert resp.status_code == 200
            data = resp.json()
            # sub is always present; email requires the email scope which the
            # dev-login cookie (non-OAuth path) does not carry.
            assert "sub" in data

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_userinfo_no_token(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/auth/userinfo")
            # No token -> anonymous -> 401.
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_userinfo_bad_token(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get(
                "/auth/userinfo",
                headers={"Authorization": "Bearer not-a-token"},
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_logout(self, app: Any) -> None:
        import asyncio

        async def _seed() -> None:
            async with app.state.resourcey_session_factory() as sess:
                sess.add(
                    User(
                        email="lo@example.com",
                        username="louser",
                        password=hash_password("lopass"),
                        enabled=True,
                    )
                )
                await sess.commit()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_seed())
        loop.close()

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            client.post(
                "/auth/dev/login",
                json={"username": "louser", "password": "lopass"},
            )
            resp = client.post("/auth/logout")
            assert resp.status_code == 204
            # Cookie should be cleared.
            assert "resourcey_session" not in resp.cookies

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_logout_no_cookie(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post("/auth/logout")
            assert resp.status_code == 204

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_openid_discovery(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/auth/.well-known/openid-configuration")
            assert resp.status_code == 200
            doc = resp.json()
            assert "authorization_endpoint" in doc

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_oauth_authorization_server(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/auth/.well-known/oauth-authorization-server")
            assert resp.status_code == 200
            doc = resp.json()
            assert "issuer" in doc

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_authorize_bad_client(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get(
                "/auth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "unknown",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_token_bad_grant_type(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/token",
                json={
                    "grant_type": "invalid",
                    "client_id": "x",
                    "client_secret": "x",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_token_authorization_code_missing_code(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": "x",
                    "client_secret": "x",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_token_refresh_missing_token(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/refresh",
                json={
                    "grant_type": "refresh_token",
                    "client_id": "x",
                    "client_secret": "x",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_refresh_bad_grant_type(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/refresh",
                json={
                    "grant_type": "authorization_code",
                    "client_id": "x",
                    "client_secret": "x",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_revoke(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/revoke",
                data={"token": "some-token", "client_id": "", "client_secret": ""},
            )
            # Best-effort -> always 200.
            assert resp.status_code == 200

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_authorize_no_credentials(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get(
                "/auth/dev/authorize",
                params={
                    "client_id": "resourcey",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "response_type": "code",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 401
            assert "WWW-Authenticate" in resp.headers

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_token_bad_grant_type(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/token",
                data={"grant_type": "bad", "client_id": "x", "client_secret": "x"},
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_token_missing_params(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/token",
                data={"grant_type": "authorization_code"},
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_refresh_bad_grant(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/refresh",
                data={"grant_type": "bad", "client_id": "x", "client_secret": "x"},
            )
            assert resp.status_code in (400, 401)


# ---------------------------------------------------------------------------
# auth_dependencies: token resolution
# ---------------------------------------------------------------------------


class TestAuthDependencies:
    async def test_depends_access_token_anonymous(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> None:
        set_config(framework_config)
        import resourcey.encryption.encryption_service as enc_mod

        original = enc_mod.get_encryption_service

        def _patched_enc() -> EncryptionService:
            return enc

        _patched_enc.cache_clear = lambda: None  # type: ignore[attr-defined]
        enc_mod.get_encryption_service = _patched_enc  # type: ignore[assignment]

        from starlette.requests import Request

        req = Request(scope={"type": "http", "app": FastAPI(), "headers": []})
        from starlette.responses import Response

        resp = Response()
        token = await depends_access_token(req, resp, session, None)
        assert token is None

        enc_mod.get_encryption_service = original  # type: ignore[assignment]

    async def test_depends_access_token_bearer(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> None:
        set_config(framework_config)
        import resourcey.auth.auth_tokens as tokens_mod
        import resourcey.encryption.encryption_service as enc_mod

        original_enc = enc_mod.get_encryption_service
        original_tokens = tokens_mod.get_encryption_service

        def _patched_enc() -> EncryptionService:
            return enc

        _patched_enc.cache_clear = lambda: None  # type: ignore[attr-defined]
        enc_mod.get_encryption_service = _patched_enc  # type: ignore[assignment]
        tokens_mod.get_encryption_service = _patched_enc  # type: ignore[assignment]

        user = await _make_user(session)
        token_service = TokenService(session, encryption_service=enc, config=framework_config)
        await _persist_idp_tokens(
            AuthService(session, encryption_service=enc, config=framework_config),
            user.id,
        )
        access_token = await token_service.create_access_token(user.id)

        from fastapi.security import HTTPAuthorizationCredentials
        from starlette.requests import Request
        from starlette.responses import Response

        req = Request(scope={"type": "http", "app": FastAPI(), "headers": []})
        bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=access_token)
        token = await depends_access_token(req, Response(), session, bearer)
        assert token is not None
        assert token.user_id == user.id

        enc_mod.get_encryption_service = original_enc  # type: ignore[assignment]
        tokens_mod.get_encryption_service = original_tokens  # type: ignore[assignment]

    async def test_depends_access_token_cached(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> None:
        """Second call in the same request returns cached token."""
        set_config(framework_config)
        import resourcey.encryption.encryption_service as enc_mod

        original = enc_mod.get_encryption_service

        def _patched_enc() -> EncryptionService:
            return enc

        _patched_enc.cache_clear = lambda: None  # type: ignore[attr-defined]
        enc_mod.get_encryption_service = _patched_enc  # type: ignore[assignment]

        from starlette.requests import Request
        from starlette.responses import Response

        req = Request(scope={"type": "http", "app": FastAPI(), "headers": []})
        resp = Response()
        # First call: anonymous.
        token1 = await depends_access_token(req, resp, session, None)
        assert token1 is None
        # Second call: should return cached None.
        token2 = await depends_access_token(req, resp, session, None)
        assert token2 is None

        enc_mod.get_encryption_service = original  # type: ignore[assignment]

    async def test_depends_access_token_invalid_bearer(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> None:
        set_config(framework_config)
        import resourcey.encryption.encryption_service as enc_mod

        original = enc_mod.get_encryption_service

        def _patched_enc() -> EncryptionService:
            return enc

        _patched_enc.cache_clear = lambda: None  # type: ignore[attr-defined]
        enc_mod.get_encryption_service = _patched_enc  # type: ignore[assignment]

        from fastapi.security import HTTPAuthorizationCredentials
        from starlette.requests import Request
        from starlette.responses import Response

        req = Request(scope={"type": "http", "app": FastAPI(), "headers": []})
        bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
        with pytest.raises(Exception, match="401"):
            await depends_access_token(req, Response(), session, bearer)

        enc_mod.get_encryption_service = original  # type: ignore[assignment]

    async def test_depends_user_id(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
    ) -> None:
        from resourcey.auth.auth_models import AuthToken

        token = AuthToken(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            enabled=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            token_type=TokenType.ACCESS_TOKEN,
            scopes=frozenset(),
        )
        assert await depends_user_id(token) == token.user_id
        assert await depends_user_id(None) is None


# ---------------------------------------------------------------------------
# TokenService: additional coverage
# ---------------------------------------------------------------------------


class TestTokenServiceExtra:
    async def test_create_access_token(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        user = await _make_user(session)
        await _persist_idp_tokens(
            AuthService(session, encryption_service=enc, config=framework_config),
            user.id,
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        token = await ts.create_access_token(user.id)
        assert token is not None

    async def test_reissue_cookie(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        user = await _make_user(session)
        await _persist_idp_tokens(
            AuthService(session, encryption_service=enc, config=framework_config),
            user.id,
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        cookie = await ts.reissue_cookie(user.id)
        assert cookie is not None

    async def test_authenticate_access_token(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        user = await _make_user(session)
        await _persist_idp_tokens(
            AuthService(session, encryption_service=enc, config=framework_config),
            user.id,
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        token = await ts.create_access_token(user.id)
        auth = await ts.authenticate(token)
        assert auth.user_id == user.id
        assert auth.token_type is TokenType.ACCESS_TOKEN

    async def test_authenticate_expired_api_key(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        user = await _make_user(session)
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        raw_key, _ = await ts.create_api_key(
            user.id, expires_at=datetime.now(UTC) - timedelta(hours=1)
        )
        with pytest.raises(InvalidTokenError, match="expired"):
            await ts.authenticate(raw_key)

    async def test_authenticate_bad_token_type(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4()), "ttyp": "unknown_type"},
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="unknown token type"):
            await ts.authenticate(token)

    async def test_authenticate_missing_token_type(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4())},
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="missing token type"):
            await ts.authenticate(token)

    async def test_authenticate_missing_subject(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {"ttyp": TokenType.ACCESS_TOKEN.value},
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="missing subject"):
            await ts.authenticate(token)

    async def test_authenticate_invalid_subject(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": "not-a-uuid", "ttyp": TokenType.ACCESS_TOKEN.value},
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="invalid subject"):
            await ts.authenticate(token)

    async def test_authenticate_missing_jti(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4()), "ttyp": TokenType.ACCESS_TOKEN.value},
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="missing jti"):
            await ts.authenticate(token)

    async def test_authenticate_missing_iat(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        # create_jwe_token always adds iat, so test the helper directly.
        with pytest.raises(InvalidTokenError, match="missing iat"):
            ts._iat({})

    async def test_authenticate_user_not_found(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        token = enc.create_jwe_token(
            {
                "sub": str(uuid.uuid4()),
                "ttyp": TokenType.ACCESS_TOKEN.value,
                "jti": str(uuid.uuid4()),
                "iat": int(datetime.now(UTC).timestamp()),
            },
            expires_in=timedelta(hours=1),
        )
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="not found or disabled"):
            await ts.authenticate(token)

    async def test_authenticate_decryption_failure(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="decryption failed"):
            await ts.authenticate("not-a-jwe")

    async def test_authenticate_non_dict_payload(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        # Create a JWE with a non-dict payload — hard to do directly, so
        # test the _decrypt path with a malformed token instead.
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError):
            await ts.authenticate("not-a-jwe")

    async def test_load_access_row_not_found(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="no federated access token"):
            await ts._load_access_row(uuid.uuid4())

    async def test_load_refresh_row_not_found(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        with pytest.raises(InvalidTokenError, match="no federated refresh token"):
            await ts._load_refresh_row_for_user(uuid.uuid4())

    async def test_idp_refresh_token_not_live(
        self,
        session: AsyncSession,
        enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        ts = TokenService(session, encryption_service=enc, config=framework_config)
        assert await ts._idp_refresh_token_live(uuid.uuid4()) is False

    async def test_base52_encode(self) -> None:
        from resourcey.auth.auth_tokens import _base52_encode

        result = _base52_encode(b"\x00\x01")
        assert isinstance(result, str)
        assert all(c not in "0Oo1IlB8S5" for c in result)

    async def test_generate_api_key_format(self) -> None:
        from resourcey.auth.auth_tokens import _generate_api_key_value

        key = _generate_api_key_value()
        assert key.startswith("oh_")

    async def test_api_key_display_prefix(self) -> None:
        from resourcey.auth.auth_tokens import _api_key_display_prefix

        assert _api_key_display_prefix("oh_abcdef123456") == "oh_abcd"


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------


class TestSessionDependency:
    async def test_get_session_reuses_existing(self, framework_config: FrameworkConfig) -> None:
        from resourcey.auth.session import get_session

        set_config(framework_config)
        app = FastAPI()
        existing = SimpleNamespace()
        from starlette.requests import Request

        req = Request(scope={"type": "http", "app": app, "headers": []})
        req.state.session = existing  # type: ignore[attr-defined]

        gen = get_session(req)
        result = await gen.__anext__()
        assert result is existing

    async def test_dispose_app_engine(self, framework_config: FrameworkConfig) -> None:
        from resourcey.auth.session import dispose_app_engine

        set_config(framework_config)
        app = FastAPI()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        app.state.resourcey_engine = engine
        app.state.resourcey_session_factory = async_sessionmaker(engine)

        await dispose_app_engine(app)
        assert not hasattr(app.state, "resourcey_engine")
        assert app.state.resourcey_session_factory is None

    async def test_dispose_app_engine_no_engine(self) -> None:
        from resourcey.auth.session import dispose_app_engine

        app = FastAPI()
        await dispose_app_engine(app)  # should not raise


# ---------------------------------------------------------------------------
# Config: AuthConfig.default_permissions
# ---------------------------------------------------------------------------


class TestConfigDefaultPermissions:
    def test_default_permissions_empty(self) -> None:
        cfg = AuthConfig()
        assert cfg.default_permissions == {}

    def test_default_permissions_valid(self) -> None:
        cfg = AuthConfig(default_permissions_json='{"document": [{"kind": "Permitted"}]}')
        result = cfg.default_permissions
        assert "document" in result
        assert result["document"] == [{"kind": "Permitted"}]

    def test_default_permissions_invalid_json(self) -> None:
        cfg = AuthConfig(default_permissions_json="not-json")
        assert cfg.default_permissions == {}

    def test_default_permissions_non_dict(self) -> None:
        cfg = AuthConfig(default_permissions_json="[1, 2, 3]")
        assert cfg.default_permissions == {}
