"""Tests for the OAuth / OIDC rung (issue #151, Part 4 of the auth roadmap).

These drive the real code paths — the real config / DB client resources, the real
``ExternalIdentity`` mapping, the real encrypted ``OAuthToken`` table and its
lease / CAS refresh, the real ``OAuthAuthenticator`` verification (with an
injected JWKS fetcher, so no network is touched), the real interactive flow
routes, and the real ``create_app`` transport. No mocks of framework code; only
the external IdP (JWKS fetch, token exchange) is faked, which is the boundary.

Covered:

* ``OAuthClient`` config + DB resources and the secret-hiding view;
* ``ExternalIdentity`` ``(issuer, subject) -> user_id`` mapping;
* ``Principal.external_id`` on the inbound path;
* ``OAuthAuthenticator`` — issuer-keyed lookup, JWKS validation, ``iss`` / ``aud``
  / ``exp`` / ``nbf`` checks, ``alg`` pinning (no HMAC confusion), fail-closed on
  an unmapped subject, and the shared principal-store (``enabled``) check;
* ``CompositeAuthenticator`` composition with an API key;
* ``OAuthTokenService`` — encrypt at rest, lease / CAS refresh serialization
  (at most one refresh, the rest observe), and refresh-failure revocation;
* ``register_oauth_routes`` — login redirect + state/PKCE, callback token
  exchange + session mint (``exp`` clamped to the IdP expiry), refresh;
* ``configure_oauth`` one-stop wiring.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.requests import Request

from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig, SessionCookieConfig
from resourcey.auth.auth_oauth import OAuthAuthenticator
from resourcey.auth.auth_oauth_client import (
    ExternalIdentityBase,
    OAuthBase,
    config_oauth_client_resource,
    external_identity_resource,
    oauth_client_view,
    oauth_token_view,
    stored_oauth_client_resource,
)
from resourcey.auth.auth_oauth_config import IdpConfig, OAuthClientConfig
from resourcey.auth.auth_oauth_provider import OAuthCredentialProvider, TokenResponse
from resourcey.auth.auth_oauth_routes import register_oauth_routes
from resourcey.auth.auth_oauth_setup import RUNG_CONFIG, RUNG_DB, configure_oauth
from resourcey.auth.auth_oauth_token import (
    OAuthTokenBase,
    create_oauth_token_tables,
    oauth_token_resource,
)
from resourcey.auth.auth_principal import (
    CompositeAuthenticator,
    Principal,
    PrincipalKind,
)
from resourcey.core.manifest import Manifest
from resourcey.encryption.encryption_config import EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService
from resourcey.http.app import create_app
from resourcey.sql.sql_resource import SqlResource

ISSUER = "https://idp.example"
JWKS_URI = "https://idp.example/jwks"
AUDIENCE = "client1"

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DISABLED_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key={"id": "t", "value": "test-secret"},  # type: ignore[arg-type]
            decryption_keys=[],
        )
    )


@pytest.fixture
def rsa_key() -> RSAKey:
    return RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})


@pytest.fixture
def jwks(rsa_key: RSAKey) -> dict[str, Any]:
    return {"keys": [rsa_key.as_dict(private=False)]}


def _client_config(**overrides: Any) -> OAuthClientConfig:
    base: dict[str, Any] = {
        "id": "c1",
        "issuer": ISSUER,
        "jwks_uri": JWKS_URI,
        "audience": AUDIENCE,
        "algorithms": ["RS256"],
        "client_id": AUDIENCE,
        "client_secret": SecretStr("shh"),
        "auth_url": "https://idp.example/authorize",
        "token_url": "https://idp.example/token",
        "refresh_url": "https://idp.example/token",
        "redirect_uri": "https://app.example/oauth/callback",
        "scopes": ["openid", "email"],
    }
    base.update(overrides)
    return OAuthClientConfig(**base)


def _idp_config(*clients: OAuthClientConfig) -> IdpConfig:
    return IdpConfig(oauth_clients=list(clients) if clients else [_client_config()])


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _token(rsa_key: RSAKey, **claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "sub": "ext-123", "aud": AUDIENCE, "exp": now + 3600, "iat": now}
    payload.update(claims)
    return jwt.encode({"alg": "RS256", "kid": "k1"}, payload, rsa_key)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(OAuthBase.metadata.create_all)
        await conn.run_sync(ExternalIdentityBase.metadata.create_all)
        await conn.run_sync(OAuthTokenBase.metadata.create_all)
        await conn.run_sync(_UserBase.metadata.create_all)
    try:
        yield maker
    finally:
        await engine.dispose()


def engine_for(base: Any) -> Any:
    """An async context manager yielding a session factory over a fresh schema."""

    class _Ctx:
        def __init__(self) -> None:
            self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        async def __aenter__(self) -> async_sessionmaker[AsyncSession]:
            async with self._engine.begin() as conn:
                await conn.run_sync(base.metadata.create_all)
            return async_sessionmaker(self._engine, expire_on_commit=False)

        async def __aexit__(self, *exc: object) -> None:
            await self._engine.dispose()

    return _Ctx()


# ---------------------------------------------------------------------------
# Principal.external_id
# ---------------------------------------------------------------------------


def test_principal_external_id_defaults_to_none() -> None:
    assert Principal.anonymous().external_id is None
    assert Principal.user(USER_ID).external_id is None
    assert Principal.user(USER_ID, external_id="auth0|abc").external_id == "auth0|abc"


# ---------------------------------------------------------------------------
# Client resources + view
# ---------------------------------------------------------------------------


def test_config_client_view_hides_the_secret() -> None:
    inner = config_oauth_client_resource(_idp_config())
    view = oauth_client_view(inner)
    read = view.get_rest_models().read_response.model_fields
    assert "client_secret" not in read
    assert "client_secret" not in view.get_queryable_fields()
    # The non-secret verification / flow fields stay readable.
    assert "issuer" in read
    assert "jwks_uri" in read


def test_db_client_view_hides_the_secret() -> None:
    inner = stored_oauth_client_resource(session_factory=object())
    view = oauth_client_view(inner)
    assert "client_secret" not in view.get_rest_models().read_response.model_fields


async def test_config_client_find_by_issuer() -> None:
    resource = config_oauth_client_resource(
        _idp_config(_client_config(id="a"), _client_config(id="b", issuer="https://other"))
    )
    service = await resource.get_service({})
    async with service:
        found = await service.find_by_issuer("https://other")
        assert found is not None and found.id == "b"
        assert await service.find_by_issuer("https://absent") is None


async def test_db_client_find_by_issuer(maker: async_sessionmaker[AsyncSession]) -> None:
    resource = stored_oauth_client_resource(session_factory=maker)
    service = await resource.get_service({})
    async with service:
        created = await service.create(
            resource.get_dto_type()(
                id="db1", issuer=ISSUER, algorithms=["RS256"], scopes=[], roles=[]
            )
        )
        assert created.id == "db1"
        found = await service.find_by_issuer(ISSUER)
        assert found is not None and found.id == "db1"


async def test_db_client_issuer_is_unique(maker: async_sessionmaker[AsyncSession]) -> None:
    """Two rows configured with the same issuer would make ``find_by_issuer``
    ambiguous (the sole selector for which JWKS a presented token is verified
    against), so the column is a unique constraint, not just an index.
    """
    from sqlalchemy.exc import IntegrityError

    resource = stored_oauth_client_resource(session_factory=maker)
    service = await resource.get_service({})
    async with service:
        await service.create(
            resource.get_dto_type()(
                id="db1", issuer=ISSUER, algorithms=["RS256"], scopes=[], roles=[]
            )
        )
        with pytest.raises(IntegrityError):
            await service.create(
                resource.get_dto_type()(
                    id="db2", issuer=ISSUER, algorithms=["RS256"], scopes=[], roles=[]
                )
            )


# ---------------------------------------------------------------------------
# ExternalIdentity
# ---------------------------------------------------------------------------


async def test_external_identity_link_and_lookup(maker: async_sessionmaker[AsyncSession]) -> None:
    resource = external_identity_resource(session_factory=maker)
    service = await resource.get_service({})
    async with service:
        linked = await service.link(ISSUER, "ext-123", USER_ID)
        assert linked == USER_ID
        # Idempotent: a second link returns the same user without a second row.
        assert await service.link(ISSUER, "ext-123", USER_ID) == USER_ID
        mapping = await service.find_by_issuer_subject(ISSUER, "ext-123")
        assert mapping is not None and mapping.user_id == USER_ID
        assert await service.find_by_issuer_subject(ISSUER, "ghost") is None
        # The same subject under a different issuer is a distinct principal.
        assert await service.find_by_issuer_subject("https://other", "ext-123") is None


# ---------------------------------------------------------------------------
# OAuthAuthenticator
# ---------------------------------------------------------------------------


async def _authenticator(
    maker: async_sessionmaker[AsyncSession],
    jwks: dict[str, Any],
    *,
    user_resource: Any = None,
    identity_resource: Any = None,
    clients: list[OAuthClientConfig] | None = None,
) -> OAuthAuthenticator:
    client_resource = config_oauth_client_resource(_idp_config(*(clients or [])))

    async def fake_get(url: str) -> dict[str, Any]:
        return jwks

    return OAuthAuthenticator(
        client_resource=client_resource,
        identity_resource=identity_resource,
        user_resource=user_resource,
        http_get=fake_get,
    )


async def test_oauth_absent_is_not_invalid(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any]
) -> None:
    auth = await _authenticator(maker, jwks)
    result = await auth.authenticate(_request({}))
    assert result.credential_present is False
    assert result.principal is None


async def test_oauth_valid_token_maps_to_internal_user(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    identity = external_identity_resource(session_factory=maker)
    async with await identity.get_service({}) as service:
        await service.link(ISSUER, "ext-123", USER_ID)
    auth = await _authenticator(maker, jwks, identity_resource=identity)
    token = _token(rsa_key, scope="openid email", roles=["viewer"], email="a@b.c")
    result = await auth.authenticate(_request({"Authorization": f"Bearer {token}"}))
    assert result.credential_valid
    assert result.principal is not None
    assert result.principal.id == USER_ID
    assert result.principal.external_id == "ext-123"
    assert result.principal.scopes == frozenset({"openid", "email"})
    assert result.principal.roles == frozenset({"viewer"})
    assert result.principal.claims == {"email": "a@b.c"}
    assert result.principal.kind is PrincipalKind.USER


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "wrong-audience"},
        {"exp": int(time.time()) - 100},
        {"nbf": int(time.time()) + 1000},
        {"iss": "https://evil.example"},
    ],
)
async def test_oauth_bad_registered_claims_are_invalid(
    maker: async_sessionmaker[AsyncSession],
    jwks: dict[str, Any],
    rsa_key: RSAKey,
    claims: dict[str, Any],
) -> None:
    auth = await _authenticator(maker, jwks)
    token = _token(rsa_key, **claims)
    result = await auth.authenticate(_request({"Authorization": f"Bearer {token}"}))
    assert result.credential_present
    assert not result.credential_valid


async def test_oauth_unknown_issuer_is_invalid(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    auth = await _authenticator(maker, jwks)
    token = _token(rsa_key, iss="https://unconfigured.example")
    result = await auth.authenticate(_request({"Authorization": f"Bearer {token}"}))
    assert result.credential_present and not result.credential_valid


async def test_oauth_alg_is_pinned_no_hmac_confusion(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    auth = await _authenticator(maker, jwks)
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"iss": ISSUER, "sub": "ext-123", "aud": AUDIENCE, "exp": now + 3600}).encode()
    )
    pem = rsa_key.as_pem(private=False)
    signature = _b64(hmac.new(pem, header + b"." + payload, hashlib.sha256).digest())
    forged = (header + b"." + payload + b"." + signature).decode()
    result = await auth.authenticate(_request({"Authorization": f"Bearer {forged}"}))
    assert result.credential_present and not result.credential_valid


async def test_oauth_unmapped_subject_is_fail_closed(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    identity = external_identity_resource(session_factory=maker)
    auth = await _authenticator(maker, jwks, identity_resource=identity)
    token = _token(rsa_key)  # a valid token, but (iss, sub) is not mapped
    result = await auth.authenticate(_request({"Authorization": f"Bearer {token}"}))
    assert result.credential_present and not result.credential_valid


async def test_oauth_without_identity_resource_is_a_service_principal(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    auth = await _authenticator(maker, jwks)  # no identity map, no principal store
    token = _token(rsa_key)
    result = await auth.authenticate(_request({"Authorization": f"Bearer {token}"}))
    assert result.credential_valid
    assert result.principal is not None
    assert result.principal.id is None
    assert result.principal.kind is PrincipalKind.SERVICE
    assert result.principal.external_id == "ext-123"


async def test_oauth_client_row_roles_are_granted(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    # A client row's ``roles`` are the simple-roles vocabulary carried by the
    # client definition; they are unioned onto the principal's roles.
    identity = external_identity_resource(session_factory=maker)
    async with await identity.get_service({}) as service:
        await service.link(ISSUER, "ext-123", USER_ID)
    auth = await _authenticator(
        maker, jwks, identity_resource=identity, clients=[_client_config(roles=["USER"])]
    )
    result = await auth.authenticate(_request({"Authorization": f"Bearer {_token(rsa_key)}"}))
    assert result.credential_valid
    assert result.principal is not None
    assert result.principal.roles == frozenset({"USER"})


class _UserBase(DeclarativeBase):
    """The declarative base for the test's principal-store table."""


class _User(_UserBase):
    """A minimal principal-store row carrying the ``enabled`` flag."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)


def _user_resource(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlResource[Any, Any]:
    """A real ``SqlResource`` over a small principal store, serving ``enabled``."""
    return SqlResource(_User, session_factory=session_factory)


async def test_oauth_user_store_is_authoritative(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    identity = external_identity_resource(session_factory=maker)
    async with await identity.get_service({}) as service:
        await service.link(ISSUER, "ext-123", USER_ID)
        await service.link(ISSUER, "ext-disabled", DISABLED_ID)
    users = _user_resource(maker)
    async with await users.get_service({}) as service:
        for user_id, is_enabled in {USER_ID: True, DISABLED_ID: False}.items():
            await service.create(users.get_dto_type()(id=user_id, enabled=is_enabled))
    auth = await _authenticator(maker, jwks, identity_resource=identity, user_resource=users)

    ok = await auth.authenticate(_request({"Authorization": f"Bearer {_token(rsa_key)}"}))
    assert ok.credential_valid

    disabled = await auth.authenticate(
        _request({"Authorization": f"Bearer {_token(rsa_key, sub='ext-disabled')}"})
    )
    assert disabled.credential_present and not disabled.credential_valid

    missing = await auth.authenticate(
        _request({"Authorization": f"Bearer {_token(rsa_key, sub='ext-missing')}"})
    )
    # ext-missing is not even mapped, so it is rejected at the mapping stage.
    assert not missing.credential_valid


async def test_composite_oauth_or_api_key(
    maker: async_sessionmaker[AsyncSession], jwks: dict[str, Any], rsa_key: RSAKey
) -> None:
    identity = external_identity_resource(session_factory=maker)
    async with await identity.get_service({}) as service:
        await service.link(ISSUER, "ext-123", USER_ID)
    oauth = await _authenticator(maker, jwks, identity_resource=identity)
    key_inner = config_api_key_resource(
        ApiKeysConfig(api_keys=[ApiKeyConfig(id="k0", key=SecretStr("secret-key"))])
    )
    api_key = ApiKeyAuthenticator(key_resource=key_inner)
    composite = CompositeAuthenticator(authenticators=[oauth, api_key])

    by_key = await composite.authenticate(_request({"X-API-Key": "secret-key"}))
    assert by_key.credential_valid
    by_oauth = await composite.authenticate(
        _request({"Authorization": f"Bearer {_token(rsa_key)}"})
    )
    assert by_oauth.credential_valid
    assert by_oauth.principal is not None and by_oauth.principal.id == USER_ID
    absent = await composite.authenticate(_request({}))
    assert not absent.credential_present
    bad = await composite.authenticate(_request({"X-API-Key": "nope"}))
    assert bad.credential_present and not bad.credential_valid


# ---------------------------------------------------------------------------
# OAuthTokenService
# ---------------------------------------------------------------------------


async def test_token_service_encrypts_at_rest(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    service = await resource.get_service({})
    async with service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="ACCESS",
            refresh_token="REFRESH",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="openid",
        )
        row = await service._row(USER_ID, "c1")
        assert row is not None
        assert "ACCESS" not in str(row["access_token"])
        assert "REFRESH" not in str(row["refresh_token"])
        token = await service.get(USER_ID, "c1")
        assert token is not None
        assert token.access_token.get_secret_value() == "ACCESS"
        assert token.refresh_token is not None
        assert token.refresh_token.get_secret_value() == "REFRESH"
        await service.revoke(USER_ID, "c1")
        assert await service.get(USER_ID, "c1") is None


async def test_token_view_is_read_only_and_hides_secrets() -> None:
    inner = oauth_token_resource(session_factory=object())
    view = oauth_token_view(inner)
    read = view.get_rest_models().read_response.model_fields
    assert "access_token" not in read
    assert "refresh_token" not in read
    assert "lease_owner" not in read
    actions = {a.value for a in view.get_supported_actions()}
    assert actions == {"read", "search", "count", "batch_read"}


async def test_refresh_lease_is_a_compare_and_swap(maker: async_sessionmaker[AsyncSession]) -> None:
    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    service = await resource.get_service({})
    async with service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="A",
            refresh_token="R",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert await service._claim(USER_ID, "c1") is True
        # A second claim while the lease is held fails; an expired lease is reclaimable.
        assert await service._claim(USER_ID, "c1") is False


class _CountingProvider(OAuthCredentialProvider):
    def __init__(self, service: Any, calls: list[Any], delay: float = 0.05) -> None:
        super().__init__(service)
        self.calls = calls
        self.delay = delay

    async def refresh(self, client: Any, refresh_token: str | None) -> TokenResponse:
        self.calls.append(refresh_token)
        await asyncio.sleep(self.delay)
        return TokenResponse(
            access_token="NEW",
            refresh_token="NEW-REFRESH",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


async def test_refresh_serializes_across_concurrent_callers(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    client = type("C", (), {"id": "c1"})()
    async with await resource.get_service({}) as service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="OLD",
            refresh_token="R",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

    calls: list[Any] = []

    async def one() -> str:
        service = await resource.get_service({})
        async with service:
            provider = _CountingProvider(service, calls)
            token = await service.refresh(principal_id=USER_ID, client=client, provider=provider)
            return token.access_token.get_secret_value()

    results = await asyncio.gather(*[one() for _ in range(5)])
    assert results == ["NEW"] * 5
    assert len(calls) == 1  # at most one refresher; the rest observed the result


async def test_store_handles_a_concurrent_first_time_insert(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A double-submitted login racing the first ``store()`` for a pair doesn't 500.

    ``store()``'s read-then-insert is not itself atomic: a second caller's
    existence check can run before the first caller's row exists, and then
    lose the insert race to the ``uq_oauth_token`` unique constraint. That is
    reproduced deterministically here (rather than relying on true
    cross-connection concurrency, which SQLite's pooling makes unreliable to
    assert on) by forcing the second ``store()`` call's own existence check to
    report "no row" even though the first call's row already landed. The
    savepoint-guarded insert must absorb the resulting ``IntegrityError`` and
    fall back to updating the row in place, not raise.
    """
    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    service = await resource.get_service({})
    async with service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="FIRST",
            refresh_token="R1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        async def stale_no_row(principal_id: UUID, client_id: str) -> None:
            return None

        service._row = stale_no_row  # type: ignore[method-assign]
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="SECOND",
            refresh_token="R2",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async with await resource.get_service({}) as service:
        token = await service.get(USER_ID, "c1")
        assert token is not None
        assert token.access_token.get_secret_value() == "SECOND"


async def test_refresh_failure_revokes_and_requires_reauth(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from resourcey.core.errors import InvalidInputError

    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    client = type("C", (), {"id": "c1"})()
    async with await resource.get_service({}) as service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="OLD",
            refresh_token="R",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

    class _Failing(OAuthCredentialProvider):
        async def refresh(self, client: Any, refresh_token: str | None) -> TokenResponse:
            raise RuntimeError("invalid_grant")

    service = await resource.get_service({})
    async with service:
        with pytest.raises(InvalidInputError):
            await service.refresh(principal_id=USER_ID, client=client, provider=_Failing(service))
    async with await resource.get_service({}) as service:
        assert await service.get(USER_ID, "c1") is None


# ---------------------------------------------------------------------------
# The interactive flow routes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _flow_app(
    maker: async_sessionmaker[AsyncSession],
    rsa_key: RSAKey,
    *,
    identity_linked: bool = True,
    idp_id_token: str | None = None,
    include_id_token: bool = True,
) -> tuple[FastAPI, IdpConfig]:
    config = _idp_config()
    client_inner = config_oauth_client_resource(config)
    token_inner = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    identity = external_identity_resource(session_factory=maker)
    jwks = {"keys": [rsa_key.as_dict(private=False)]}

    async def fake_get(url: str) -> dict[str, Any]:
        return jwks

    async def fake_post(
        url: str, *, data: dict[str, str], headers: dict[str, str]
    ) -> _FakeResponse:
        payload: dict[str, Any] = {
            # Deliberately opaque (unlike a JWT): proves the callback resolves
            # identity from the ID token, never the access token.
            "access_token": "opaque-provider-access-token",
            "refresh_token": "IDP-REFRESH",
            "expires_in": 120,
            "scope": "openid",
        }
        if include_id_token:
            payload["id_token"] = idp_id_token or _token(rsa_key)
        return _FakeResponse(payload)

    app = create_app(Manifest(resources=[]), dependency_builder=None)
    register_oauth_routes(
        app,
        client_inner,
        token_resource=token_inner,
        identity_resource=identity,
        config=config,
        session_config=SessionCookieConfig(
            session_cookie_secure=False, session_cookie_ttl_seconds=999999
        ),
        encryption_service=_encryption(),
        http_post=fake_post,
        http_get=fake_get,
    )
    return app, config


async def _seed_identity(maker: async_sessionmaker[AsyncSession]) -> None:
    resource = external_identity_resource(session_factory=maker)
    async with await resource.get_service({}) as service:
        await service.link(ISSUER, "ext-123", USER_ID)


async def test_login_redirects_with_state_and_pkce(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    app, config = _flow_app(maker, rsa_key)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        response = await c.get("/oauth/login", params={"client": "c1"})
        assert response.status_code == 307
        location = response.headers["location"]
        assert location.startswith("https://idp.example/authorize?")
        assert "code_challenge=" in location
        assert "code_challenge_method=S256" in location
        assert "state=" in location
        assert config.oauth_state_cookie_name in response.cookies


async def test_callback_exchanges_mints_session_and_stores_token(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    await _seed_identity(maker)
    app, _ = _flow_app(maker, rsa_key)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login = await c.get("/oauth/login", params={"client": "c1"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        callback = await c.get("/oauth/callback", params={"code": "abc", "state": state})
        assert callback.status_code == 200, callback.text
        assert "session" in callback.cookies
        # The stored token is encrypted at rest and decrypts to the IdP token.
        resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
        async with await resource.get_service({}) as service:
            token = await service.get(USER_ID, "c1")
        assert token is not None
        assert token.refresh_token is not None
        assert token.refresh_token.get_secret_value() == "IDP-REFRESH"


async def test_callback_rejects_a_state_mismatch(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    app, _ = _flow_app(maker, rsa_key)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        await c.get("/oauth/login", params={"client": "c1"})
        response = await c.get("/oauth/callback", params={"code": "abc", "state": "wrong"})
        assert response.status_code == 400


async def test_callback_rejects_unlinked_identity(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    # No ExternalIdentity seeded -> fail-closed.
    app, _ = _flow_app(maker, rsa_key)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login = await c.get("/oauth/login", params={"client": "c1"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        response = await c.get("/oauth/callback", params={"code": "abc", "state": state})
        assert response.status_code == 400


async def test_login_requests_openid_scope_even_when_not_configured() -> None:
    """A client configured without ``openid`` still gets an ID token on callback.

    Login always requests ``openid`` in addition to the client's configured
    scopes, so the provider includes an ID token in the token response — the
    callback's identity resolution depends on it.
    """
    config = _idp_config(_client_config(scopes=["email"]))
    inner = config_oauth_client_resource(config)
    app = create_app(Manifest(resources=[]), dependency_builder=None)
    register_oauth_routes(
        app, inner, config=config, session_config=SessionCookieConfig(session_cookie_secure=False)
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        response = await c.get("/oauth/login", params={"client": "c1"})
        location = response.headers["location"]
        scope_param = location.split("scope=")[1].split("&")[0]
        requested = unquote_plus(scope_param).split(" ")
        assert "openid" in requested
        assert "email" in requested


async def test_callback_rejects_a_missing_id_token(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    """An access-token-only response (a common real-world shape) fails closed.

    The provider's access token is opaque and never used for identity; if it
    returns no ``id_token`` the callback must reject the login rather than
    silently fall back to (unverified) access-token claims.
    """
    await _seed_identity(maker)
    app, _ = _flow_app(maker, rsa_key, include_id_token=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login = await c.get("/oauth/login", params={"client": "c1"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        response = await c.get("/oauth/callback", params={"code": "abc", "state": state})
        assert response.status_code == 400
        assert "ID token" in response.text


async def test_callback_rejects_an_id_token_with_the_wrong_audience(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    """A verifiable-but-wrong-audience ID token fails verification, not identity lookup.

    Proves the callback actually **verifies** the ID token (signature + ``iss``
    / ``aud``) through the authenticator's JWKS path rather than trusting an
    unverified decode.
    """
    await _seed_identity(maker)
    bad_token = _token(rsa_key, aud="someone-else")
    app, _ = _flow_app(maker, rsa_key, idp_id_token=bad_token)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login = await c.get("/oauth/login", params={"client": "c1"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        response = await c.get("/oauth/callback", params={"code": "abc", "state": state})
        assert response.status_code == 400
        assert "verification" in response.text


async def test_refresh_route_uses_the_stored_token(
    maker: async_sessionmaker[AsyncSession], rsa_key: RSAKey
) -> None:
    await _seed_identity(maker)
    app, _ = _flow_app(maker, rsa_key)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login = await c.get("/oauth/login", params={"client": "c1"})
        state = login.headers["location"].split("state=")[1].split("&")[0]
        await c.get("/oauth/callback", params={"code": "abc", "state": state})
        response = await c.post("/oauth/refresh", params={"client": "c1"})
        assert response.status_code == 200, response.text
        assert "session" in response.cookies


# ---------------------------------------------------------------------------
# configure_oauth
# ---------------------------------------------------------------------------


def test_configure_oauth_config_rung() -> None:
    setup = configure_oauth(_idp_config(), rung=RUNG_CONFIG)
    assert setup.client_resource is not None
    assert isinstance(setup.authenticator, OAuthAuthenticator)
    resources = setup.resources()
    assert setup.client_view in resources
    assert setup.identity_resource in resources
    # The token view is debug-only: not exposed by default.
    assert setup.token_view not in resources
    assert setup.token_view in setup.resources(expose_token=True)


def test_configure_oauth_db_rung(maker: async_sessionmaker[AsyncSession]) -> None:
    setup = configure_oauth(_idp_config(), rung=RUNG_DB, session_factory=maker)
    assert isinstance(setup.authenticator, OAuthAuthenticator)
    assert setup.client_resource.get_resource_path() == "oauth-clients"


async def test_configure_oauth_creates_tables(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    new_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await create_oauth_token_tables(new_maker)
        # All three tables exist and are queryable.
        from sqlalchemy import select

        from resourcey.auth.auth_oauth_client import ExternalIdentity, OAuthClient
        from resourcey.auth.auth_oauth_token import OAuthToken

        async with new_maker() as session:
            for model in (OAuthClient, ExternalIdentity, OAuthToken):
                await session.execute(select(model))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# IdpConfig env parsing
# ---------------------------------------------------------------------------


def test_idp_config_parses_clients_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from resourcey.config.config_base import _reset_config_prefix

    _reset_config_prefix()
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_ID", "env1")
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_ISSUER", ISSUER)
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_ALGORITHMS", '["RS256"]')
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_SCOPES", '["openid"]')
    monkeypatch.setenv("APP_OAUTH_CLIENTS_0_ROLES", '["viewer"]')
    try:
        config = IdpConfig.get_instance()
        assert len(config.oauth_clients) == 1
        entry = config.oauth_clients[0]
        assert entry.id == "env1"
        assert entry.issuer == ISSUER
        assert entry.algorithms == ["RS256"]
        assert entry.roles == ["viewer"]
    finally:
        IdpConfig.clear_instance_cache()
        _reset_config_prefix()


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


# ---------------------------------------------------------------------------
# CredentialProvider — the outbound path
# ---------------------------------------------------------------------------


async def test_authorize_url_includes_scopes_and_pkce() -> None:
    from resourcey.auth.auth_oauth_provider import authorize_url

    record = _client_config()
    url = authorize_url(record, state="s", redirect_uri="https://app/cb", code_challenge="chal")
    assert url.startswith("https://idp.example/authorize?")
    assert "client_id=client1" in url
    assert "scope=openid+email" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "state=s" in url


async def test_authorize_url_requires_an_auth_url() -> None:
    from resourcey.auth.auth_oauth_provider import authorize_url
    from resourcey.core.errors import InvalidInputError

    with pytest.raises(InvalidInputError):
        authorize_url(_client_config(auth_url=None), state="s", redirect_uri="https://app/cb")


async def test_credential_provider_refresh_and_exchange(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    record = _client_config()
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: dict[str, str], headers: dict[str, str]) -> _FakeResponse:
        posts.append({"url": url, "data": data})
        return _FakeResponse(
            {"access_token": "AT", "refresh_token": "RT", "expires_in": 60, "scope": "openid"}
        )

    service = await oauth_token_resource(
        session_factory=maker, encryption_service=_encryption()
    ).get_service({})
    provider = OAuthCredentialProvider(service, fake_post)
    refreshed = await provider.refresh(record, "OLD-REFRESH")
    assert refreshed.access_token == "AT"
    assert posts[-1]["data"]["grant_type"] == "refresh_token"
    assert posts[-1]["data"]["client_secret"] == "shh"
    exchanged = await provider.exchange_code(
        record, code="CODE", redirect_uri="https://app/cb", code_verifier="V"
    )
    assert exchanged.access_token == "AT"
    assert posts[-1]["data"]["code"] == "CODE"
    assert posts[-1]["data"]["code_verifier"] == "V"


async def test_credential_provider_access_token_returns_a_live_token(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    record = _client_config()
    resource = oauth_token_resource(session_factory=maker, encryption_service=_encryption())
    async with await resource.get_service({}) as service:
        await service.store(
            principal_id=USER_ID,
            client_id="c1",
            access_token="LIVE",
            refresh_token="R",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    service = await resource.get_service({})
    provider = OAuthCredentialProvider(service, None)
    principal = Principal.user(USER_ID)
    async with service:
        assert await provider.access_token(principal, record) == "LIVE"


async def test_credential_provider_rejects_an_anonymous_principal(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from resourcey.core.errors import InvalidInputError

    record = _client_config()
    service = await oauth_token_resource(
        session_factory=maker, encryption_service=_encryption()
    ).get_service({})
    provider = OAuthCredentialProvider(service, None)
    with pytest.raises(InvalidInputError):
        await provider.access_token(Principal.anonymous(), record)


def test_token_response_requires_an_access_token() -> None:
    from resourcey.auth.auth_oauth_provider import TokenResponse
    from resourcey.core.errors import InvalidInputError

    with pytest.raises(InvalidInputError):
        TokenResponse.from_payload({"refresh_token": "R"})


# ---------------------------------------------------------------------------
# Principal store reuse — the API-key path delegates to the shared helper
# ---------------------------------------------------------------------------


class _PlainUserBase(DeclarativeBase):
    """A base for an identity-only principal store (no ``enabled`` attribute)."""


class _PlainUser(_PlainUserBase):
    """A principal-store row with no ``enabled`` flag."""

    __tablename__ = "plain_users"

    id: Mapped[UUID] = mapped_column(primary_key=True)


async def test_api_key_principal_store_check_is_shared(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The shared ``principal_is_active`` keeps the API-key path's posture."""
    from resourcey.auth.auth_principal import principal_is_active

    users = _user_resource(maker)
    async with await users.get_service({}) as service:
        await service.create(users.get_dto_type()(id=USER_ID, enabled=False))
    principal = Principal.user(USER_ID)
    assert await principal_is_active(users, principal) is False
    # No store trusts the credential.
    assert await principal_is_active(None, principal) is True
    # A store row lacking ``enabled`` (an identity-only store) is treated enabled,
    # while a missing row is a rejection.
    async with engine_for(_PlainUserBase) as plain:
        plain_resource = SqlResource(_PlainUser, session_factory=plain)
        async with await plain_resource.get_service({}) as service:
            await service.create(plain_resource.get_dto_type()(id=USER_ID))
        assert await principal_is_active(plain_resource, principal) is True
        assert await principal_is_active(plain_resource, Principal.user(DISABLED_ID)) is False
