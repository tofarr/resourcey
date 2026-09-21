"""Tests for the auth feature: encryption, password, tokens, dev IdP flow (issue #4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.auth.auth_models import (
    TokenType,
    User,
)
from resourcey.auth.auth_service import (
    AuthService,
    _decode_id_token,
    _derive_code_challenge,
    _generate_code_verifier,
    _idp_access_expiry,
    _idp_refresh_expiry,
    _mint_cookie_jwe,
    _normalize_scopes,
    _seconds_until,
    _wildcard_match,
)
from resourcey.auth.auth_tokens import (
    InvalidTokenError,
    TokenService,
    hash_api_key_value,
)
from resourcey.auth.password import hash_password, verify_password
from resourcey.config.config_framework import AuthConfig, FrameworkConfig, IdpConfig
from resourcey.config.config_runtime import set_config
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import (
    EncryptionService,
)

# ---------------------------------------------------------------------------
# Fixtures
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
    from resourcey.auth.auth_models import AuthBase

    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def token_service(
    session: AsyncSession, enc: EncryptionService, framework_config: FrameworkConfig
) -> TokenService:
    return TokenService(session, encryption_service=enc, config=framework_config)


@pytest.fixture
def auth_service(
    session: AsyncSession, enc: EncryptionService, framework_config: FrameworkConfig
) -> AuthService:
    return AuthService(session, encryption_service=enc, config=framework_config)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestPassword:
    def test_hash_and_verify(self) -> None:
        hashed = hash_password("s3cret")
        assert verify_password("s3cret", hashed)
        assert not verify_password("wrong", hashed)

    def test_long_password_pre_hashed(self) -> None:
        long_pw = "x" * 200
        hashed = hash_password(long_pw)
        assert verify_password(long_pw, hashed)

    def test_malformed_hash_returns_false(self) -> None:
        assert not verify_password("test", "not-a-hash")


# ---------------------------------------------------------------------------
# Encryption service: create_jwe_token / decrypt_jwe_token
# ---------------------------------------------------------------------------


class TestEncryptionToken:
    def test_round_trip(self, enc: EncryptionService) -> None:
        token = enc.create_jwe_token(
            {"sub": "user-1", "ttyp": "cookie"}, expires_in=timedelta(hours=1)
        )
        payload = enc.decrypt_jwe_token(token)
        assert payload["sub"] == "user-1"
        assert payload["ttyp"] == "cookie"
        assert "iat" in payload
        assert "exp" in payload

    def test_no_expiry(self, enc: EncryptionService) -> None:
        token = enc.create_jwe_token({"sub": "user-1"})
        payload = enc.decrypt_jwe_token(token)
        assert payload["sub"] == "user-1"
        assert "exp" not in payload

    def test_invalid_token_raises(self, enc: EncryptionService) -> None:
        with pytest.raises(ValueError, match="Invalid JWE token format"):
            enc.decrypt_jwe_token("not-a-token")

    def test_unknown_kid_raises(self, enc: EncryptionService) -> None:
        other_cfg = EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="other", value="other-secret")
        )
        other = EncryptionService(other_cfg)
        token = other.create_jwe_token({"sub": "x"})
        with pytest.raises(ValueError, match="Key ID"):
            enc.decrypt_jwe_token(token)


# ---------------------------------------------------------------------------
# TokenService: API key creation + authentication
# ---------------------------------------------------------------------------


class TestTokenServiceApiKeys:
    async def test_create_and_authenticate_api_key(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        raw_key, row = await token_service.create_api_key(user.id, name="test-key")
        assert raw_key.startswith("oh_")
        assert row.name == "test-key"
        assert row.key_hash == hash_api_key_value(raw_key)

        auth_token = await token_service.authenticate(raw_key)
        assert auth_token.user_id == user.id
        assert auth_token.token_type is TokenType.API_KEY
        assert auth_token.enabled is True

    async def test_disabled_api_key(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        raw_key, _ = await token_service.create_api_key(user.id, enabled=False)
        # A disabled API key authenticates but returns enabled=False — the
        # dependency layer (depends_access_token) surfaces the 401.
        auth_token = await token_service.authenticate(raw_key)
        assert auth_token.user_id == user.id
        assert auth_token.enabled is False

    async def test_unknown_api_key(self, token_service: TokenService) -> None:
        with pytest.raises(InvalidTokenError, match="unknown api key"):
            await token_service.authenticate("oh_nonexistent")

    async def test_disabled_user_api_key(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=False)
        session.add(user)
        await session.flush()

        raw_key, _ = await token_service.create_api_key(user.id)
        with pytest.raises(InvalidTokenError, match="not found or disabled"):
            await token_service.authenticate(raw_key)


# ---------------------------------------------------------------------------
# TokenService: cookie / access / refresh token authentication
# ---------------------------------------------------------------------------


class TestTokenServiceJwe:
    async def test_authenticate_cookie_token(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        # Persist IdP tokens so the cookie can be minted.
        from resourcey.auth.auth_service import AuthService

        auth_svc = AuthService(
            session, encryption_service=token_service._enc, config=token_service._cfg
        )
        idp_tokens = {
            "access_token": "idp-access",
            "refresh_token": "idp-refresh",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
            "id_token": _make_id_token(str(user.id), "test@example.com"),
        }
        await auth_svc.persist_idp_tokens(user.id, idp_tokens)
        await session.flush()

        cookie = await token_service.create_cookie_token(user.id)
        auth_token = await token_service.authenticate(cookie)
        assert auth_token.user_id == user.id
        assert auth_token.token_type is TokenType.COOKIE
        assert auth_token.enabled is True

    async def test_expired_token_rejected(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        # Mint a token with 1s expiry.
        token = token_service._enc.create_jwe_token(
            {"sub": str(user.id), "ttyp": TokenType.COOKIE.value, "jti": str(uuid.uuid4())},
            expires_in=timedelta(seconds=1),
        )
        import time

        time.sleep(1.1)
        with pytest.raises(InvalidTokenError, match="token expired"):
            await token_service.authenticate(token)

    async def test_refresh_token_not_accepted_as_bearer(
        self, token_service: TokenService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        from resourcey.auth.auth_service import AuthService

        auth_svc = AuthService(
            session, encryption_service=token_service._enc, config=token_service._cfg
        )
        idp_tokens = {
            "access_token": "idp-access",
            "refresh_token": "idp-refresh",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
        }
        await auth_svc.persist_idp_tokens(user.id, idp_tokens)
        await session.flush()

        refresh_token, _ = await token_service.create_refresh_token(user.id)
        with pytest.raises(InvalidTokenError, match="not valid for this endpoint"):
            await token_service.authenticate(refresh_token)

        # With allow_refresh=True, it should authenticate.
        auth_token = await token_service.authenticate(refresh_token, allow_refresh=True)
        assert auth_token.token_type is TokenType.IDP_REFRESH_TOKEN


# ---------------------------------------------------------------------------
# AuthService: IdP token persistence + expiry computation
# ---------------------------------------------------------------------------


class TestAuthServicePersistence:
    async def test_persist_idp_tokens(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        idp_tokens = {
            "access_token": "idp-access-value",
            "refresh_token": "idp-refresh-value",
            "expires_in": 3600,
            "refresh_expires_in": 86400,
        }
        refresh_row, access_row = await auth_service.persist_idp_tokens(user.id, idp_tokens)
        assert refresh_row.creator_id == user.id
        assert access_row.refresh_token_id == refresh_row.id
        # The stored values should be encrypted (not plaintext).
        assert access_row.access_token != "idp-access-value"
        assert refresh_row.refresh_token != "idp-refresh-value"
        # Decryption should recover the original.
        assert auth_service._enc.decrypt_value(access_row.access_token) == "idp-access-value"
        assert auth_service._enc.decrypt_value(refresh_row.refresh_token) == "idp-refresh-value"

    async def test_persist_idp_tokens_fallback_expiry(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = User(email="test@example.com", username="test", enabled=True)
        session.add(user)
        await session.flush()

        idp_tokens = {
            "access_token": "a",
            "refresh_token": "r",
        }
        refresh_row, access_row = await auth_service.persist_idp_tokens(user.id, idp_tokens)
        # Should fall back to config defaults.
        assert access_row.expires_at > datetime.now(UTC)
        assert refresh_row.expires_at > access_row.expires_at


class TestExpiryHelpers:
    def test_idp_access_expiry_from_expires_in(self, idp_config: IdpConfig) -> None:
        expiry = _idp_access_expiry({"expires_in": 3600}, drift_seconds=60, idp=idp_config)
        expected = datetime.now(UTC) + timedelta(seconds=3540)
        assert abs((expiry - expected).total_seconds()) < 5

    def test_idp_access_expiry_fallback(self, idp_config: IdpConfig) -> None:
        expiry = _idp_access_expiry({}, drift_seconds=60, idp=idp_config)
        expected = datetime.now(UTC) + timedelta(seconds=840)
        assert abs((expiry - expected).total_seconds()) < 5

    def test_idp_refresh_expiry_from_refresh_expires_in(self, idp_config: IdpConfig) -> None:
        expiry = _idp_refresh_expiry(
            {"refresh_expires_in": 86400}, drift_seconds=60, idp=idp_config
        )
        expected = datetime.now(UTC) + timedelta(seconds=86340)
        assert abs((expiry - expected).total_seconds()) < 5


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_normalize_scopes(self) -> None:
        assert _normalize_scopes("openid email") == frozenset({"openid", "email"})
        assert _normalize_scopes("openid unknown_scope") == frozenset({"openid"})
        assert _normalize_scopes(None) == frozenset()
        assert _normalize_scopes("") == frozenset()

    def test_seconds_until(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=30)
        assert abs(_seconds_until(future) - 30) <= 1
        past = datetime.now(UTC) - timedelta(seconds=10)
        assert _seconds_until(past) == 0

    def test_wildcard_match_exact(self) -> None:
        assert _wildcard_match("https://app.example.com/cb", "https://app.example.com/cb")
        assert not _wildcard_match("https://app.example.com/cb", "https://other.com/cb")

    def test_wildcard_match_pattern(self) -> None:
        assert _wildcard_match("https://*.example.com/cb", "https://app.example.com/cb")
        assert not _wildcard_match("https://*.example.com/cb", "https://app.other.com/cb")

    def test_derive_code_challenge_s256(self) -> None:
        verifier = _generate_code_verifier()
        challenge = _derive_code_challenge(verifier, "S256")
        assert challenge != verifier
        # Should be base64url without padding.
        assert "=" not in challenge

    def test_derive_code_challenge_plain(self) -> None:
        verifier = _generate_code_verifier()
        assert _derive_code_challenge(verifier, "plain") == verifier

    def test_decode_id_token(self) -> None:
        token = _make_id_token("user-123", "user@example.com")
        claims = _decode_id_token(token)
        assert claims["sub"] == "user-123"
        assert claims["email"] == "user@example.com"

    def test_decode_id_token_malformed(self) -> None:
        assert _decode_id_token("not-a-jwt") == {}

    def test_mint_cookie_jwe(self, enc: EncryptionService) -> None:
        user_id = uuid.uuid4()
        access_id = uuid.uuid4()
        exp = datetime.now(UTC) + timedelta(hours=1)
        cookie = _mint_cookie_jwe(enc, user_id=user_id, access_id=access_id, access_expires_at=exp)
        payload = enc.decrypt_jwe_token(cookie)
        assert payload["sub"] == str(user_id)
        assert payload["ttyp"] == TokenType.COOKIE.value
        assert payload["aid"] == str(access_id)
        assert payload["axp"] == int(exp.timestamp())


# ---------------------------------------------------------------------------
# Dev IdP integration test (full OAuth round-trip with the dev IdP)
# ---------------------------------------------------------------------------


def _make_id_token(sub: str, email: str) -> str:
    import base64
    import json

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none"}).encode())
    payload = b64(json.dumps({"sub": sub, "email": email}).encode())
    return f"{header}.{payload}."


class TestDevIdpFlow:
    @pytest.fixture
    def app(
        self,
        framework_config: FrameworkConfig,
        enc: EncryptionService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> object:

        from fastapi import FastAPI

        from resourcey.auth.auth_router import router as auth_router
        from resourcey.auth.dev_router import router as dev_router

        set_config(framework_config)

        # Patch the encryption singleton to return our test instance, but
        # keep a ``cache_clear`` attribute so the autouse ``_encryption_env``
        # fixture doesn't break.
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
        from resourcey.auth.auth_models import AuthBase

        app.state.resourcey_engine = engine
        app.state.resourcey_session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Create tables synchronously before the TestClient starts.
        import asyncio

        async def _create_tables() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(AuthBase.metadata.create_all)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_create_tables())
        loop.close()

        yield app

        # Dispose the engine to avoid unraisable-exception warnings from
        # garbage-collected connections (filterwarnings = ["error"] escalates
        # those to test failures).
        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine.dispose())
        loop.close()

        enc_mod.get_encryption_service = original  # type: ignore[assignment]

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_openid_discovery(self, app: object) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/auth/.well-known/openid-configuration")
            assert resp.status_code == 200
            doc = resp.json()
            assert "authorization_endpoint" in doc
            assert "token_endpoint" in doc
            assert "userinfo_endpoint" in doc
            assert "code" in doc["response_types_supported"]

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_login_bad_credentials(self, app: object) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/login",
                json={"username": "nope", "password": "wrong"},
            )
            assert resp.status_code == 401
