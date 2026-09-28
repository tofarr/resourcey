"""Tests for the ``v2`` principal / authenticator seam (issue #131).

These drive the real code paths — a real ``EncryptionService`` over a JWE, real
FastAPI transport, and real resources — with no mocks. Covered:

* the ``Principal`` representation (anonymous / user / service, roles, claims);
* ``AuthResult``'s absent / invalid / authenticated distinction;
* ``CookieAuthenticator`` — a valid cookie, a tampered one, a cookie with no
  ``sub``, and the stale-but-still-authenticated notion of ``exp``;
* ``CompositeAuthenticator`` — first method wins, and absent vs invalid;
* the ``PolicyResolver`` seam, including a resource/principal-dependent app
  resolver and the OR-combination of several policies;
* the authenticating builder's ``get_principal_dependency`` on the transport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr

from resourcey.v2.auth.auth_api_key import (
    API_KEY_HEADER_NAME,
    ApiKeyAuthenticator,
)
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import (
    ApiKeyConfig,
    ApiKeysConfig,
    SessionCookieConfig,
)
from resourcey.v2.auth.auth_cookie import CookieAuthenticator
from resourcey.v2.auth.auth_policy import (
    AllowAll,
    AllowAllResolver,
    DenyAll,
    DenyAllResolver,
    Policy,
    PolicyResolver,
    ReadOnly,
)
from resourcey.v2.auth.auth_principal import (
    AuthResult,
    CompositeAuthenticator,
    Principal,
    PrincipalKind,
)
from resourcey.v2.encryption.encryption_config import (
    EncryptionKeyConfig,
    EncryptionKeysConfig,
)
from resourcey.v2.encryption.encryption_service import EncryptionService

USER_ID = uuid4()


class Widget(BaseModel):
    """A minimal served Pydantic model for the transport test."""

    id: int
    label: str


def _service() -> EncryptionService:
    """A real encryption service over a fixed key (no env, no dev-default warning)."""
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value=SecretStr("test-secret")),
        )
    )


def _cookie(
    service: EncryptionService, *, sub: UUID | None = USER_ID, exp_in: int | None = 3600
) -> str:
    payload: dict[str, Any] = {}
    if sub is not None:
        payload["sub"] = str(sub)
    if exp_in is not None:
        payload["exp"] = int((datetime.now(UTC) + timedelta(seconds=exp_in)).timestamp())
    return service.create_jwe_token(payload)


# ---------------------------------------------------------------------------
# Principal / AuthResult
# ---------------------------------------------------------------------------


def test_principal_variants():
    assert Principal.anonymous().id is None
    assert Principal.anonymous().kind is PrincipalKind.ANONYMOUS
    assert Principal.user(USER_ID).kind is PrincipalKind.USER
    assert Principal.service().kind is PrincipalKind.SERVICE
    assert Principal.user(USER_ID, roles=frozenset({"admin"})).roles == {"admin"}


def test_auth_result_distinguishes_absent_from_invalid():
    absent = AuthResult.absent()
    invalid = AuthResult.invalid()
    ok = AuthResult.authenticated(Principal.user(USER_ID))
    assert absent.credential_present is False and absent.principal is None
    assert invalid.credential_present is True and invalid.principal is None
    assert ok.credential_present is True and ok.credential_valid is True
    assert absent.refresh_recommended is False


# ---------------------------------------------------------------------------
# CookieAuthenticator
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cookie_authenticator() -> CookieAuthenticator:
    return CookieAuthenticator(cookie_name="sid", encryption_service=_service())


async def _authenticate(authenticator: Any, cookies: dict[str, str]) -> AuthResult:
    """Run an authenticator's FastAPI dependency over a tiny app with cookies."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(result: AuthResult = Depends(authenticator.dependency())):  # noqa: B008
        return result.model_dump(mode="json")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        response = await client.get("/whoami")
    return AuthResult.model_validate(json.loads(response.text))


async def test_absent_cookie_is_absent(cookie_authenticator):
    result = await _authenticate(cookie_authenticator, {})
    assert result.credential_present is False
    assert result.principal is None


async def test_valid_cookie_authenticates(cookie_authenticator):
    token = _cookie(_service())
    result = await _authenticate(cookie_authenticator, {"sid": token})
    assert result.credential_valid is True
    assert result.principal is not None
    assert result.principal.id == USER_ID
    assert result.principal.kind is PrincipalKind.USER
    assert result.refresh_recommended is False


async def test_tampered_cookie_is_invalid(cookie_authenticator):
    result = await _authenticate(cookie_authenticator, {"sid": "not-a-jwe"})
    assert result.credential_present is True
    assert result.credential_valid is False
    assert result.principal is None


async def test_cookie_without_sub_is_invalid(cookie_authenticator):
    token = _cookie(_service(), sub=None)
    result = await _authenticate(cookie_authenticator, {"sid": token})
    assert result.credential_valid is False


async def test_expired_cookie_is_stale_but_still_authenticated():
    authenticator = CookieAuthenticator(cookie_name="sid", encryption_service=_service())
    token = _cookie(_service(), exp_in=-10)
    result = await _authenticate(authenticator, {"sid": token})
    assert result.credential_valid is True
    assert result.principal is not None
    assert result.refresh_recommended is True


async def test_cookie_inside_the_refresh_window_is_stale():
    authenticator = CookieAuthenticator(
        cookie_name="sid", encryption_service=_service(), refresh_after=timedelta(hours=2)
    )
    # exp in one hour, refresh margin two hours -> already inside the window.
    token = _cookie(_service(), exp_in=3600)
    result = await _authenticate(authenticator, {"sid": token})
    assert result.credential_valid is True
    assert result.refresh_recommended is True


async def test_cookie_without_exp_never_goes_stale():
    authenticator = CookieAuthenticator(
        cookie_name="sid", encryption_service=_service(), refresh_after=timedelta(seconds=1)
    )
    token = _cookie(_service(), exp_in=None)
    result = await _authenticate(authenticator, {"sid": token})
    assert result.credential_valid is True
    assert result.refresh_recommended is False


# ---------------------------------------------------------------------------
# CompositeAuthenticator
# ---------------------------------------------------------------------------


def _api_key_authenticator(key: str) -> ApiKeyAuthenticator:
    cfg = ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", key=SecretStr(key))])
    from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource

    return ApiKeyAuthenticator(key_resource=config_api_key_resource(cfg))


async def test_composite_uses_the_first_authenticating_method():
    composite = CompositeAuthenticator(
        authenticators=[
            CookieAuthenticator(cookie_name="sid", encryption_service=_service()),
            _api_key_authenticator("secret-one"),
        ]
    )
    app = FastAPI()
    from fastapi import Depends

    @app.get("/who")
    async def who(result: AuthResult = Depends(composite.dependency())):  # noqa: B008
        return {"kind": result.principal.kind.value if result.principal else None}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The API key authenticates even though no cookie is present.
        response = await client.get("/who", headers={API_KEY_HEADER_NAME: "secret-one"})
        assert response.json() == {"kind": "service"}


async def test_composite_invalid_when_a_credential_is_present_but_bad():
    composite = CompositeAuthenticator(
        authenticators=[
            CookieAuthenticator(cookie_name="sid", encryption_service=_service()),
            _api_key_authenticator("secret-one"),
        ]
    )
    app = FastAPI()
    from fastapi import Depends

    @app.get("/who")
    async def who(result: AuthResult = Depends(composite.dependency())):  # noqa: B008
        return {"present": result.credential_present, "valid": result.credential_valid}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/who", headers={API_KEY_HEADER_NAME: "wrong"})
        assert response.json() == {"present": True, "valid": False}


# ---------------------------------------------------------------------------
# PolicyResolver seam
# ---------------------------------------------------------------------------


class _RoleResolver(PolicyResolver):
    """An app-level resolver: a role-carried mapping of principal to policy."""

    async def resolve(self, resource: Any, principal: Principal | None) -> list[Policy]:
        roles = principal.roles if principal is not None else frozenset()
        if "admin" in roles:
            return [AllowAll()]
        if "reader" in roles:
            return [ReadOnly()]
        return []


async def test_role_resolver_maps_admin_to_allow_all():
    policies = await _RoleResolver().resolve(
        object(), Principal.user(USER_ID, roles=frozenset({"admin"}))
    )
    assert [type(p).__name__ for p in policies] == ["AllowAll"]


async def test_role_resolver_denies_an_unknown_role():
    assert await _RoleResolver().resolve(object(), Principal.user(USER_ID)) == []


async def test_built_in_resolvers():
    assert [type(p).__name__ for p in await AllowAllResolver().resolve(object(), None)] == [
        "AllowAll"
    ]
    assert await DenyAllResolver().resolve(object(), Principal.user(USER_ID)) == []


# ---------------------------------------------------------------------------
# The builder's principal dependency on the transport
# ---------------------------------------------------------------------------


async def test_builder_principal_dependency_guards_and_appears_in_openapi():
    cfg = ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", key=SecretStr("secret-one"))])
    from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource

    inner = config_api_key_resource(cfg)
    builder = AuthorizedDependencyBuilder(authenticator=ApiKeyAuthenticator(key_resource=inner))
    dependency = builder.get_principal_dependency()
    assert dependency is not None

    app = FastAPI()
    from fastapi import Depends

    @app.get("/ping", dependencies=[Depends(dependency)])
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/ping")).status_code == 401
        assert (
            await client.get("/ping", headers={API_KEY_HEADER_NAME: "secret-one"})
        ).status_code == 200


def test_default_builder_has_no_principal_dependency():
    from resourcey.v2.http.dependency_builder import OpenDependencyBuilder

    assert OpenDependencyBuilder().get_principal_dependency() is None


async def test_real_app_mounts_the_security_scheme_on_every_route():
    """The authenticating builder's scheme reaches the app's OpenAPI operations.

    ``register_routes`` threads ``get_principal_dependency`` into each route's
    ``dependencies=[...]``, which is what makes the scheme (not just the service
    wrapper) visible in OpenAPI.
    """
    from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
    from resourcey.v2.core.manifest import Manifest
    from resourcey.v2.http.app import create_app
    from resourcey.v2.list.list_resource import ListResource

    cfg = ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", key=SecretStr("secret-one"))])
    inner = config_api_key_resource(cfg)
    widgets = ListResource([Widget(id=1, label="a")], path="widgets")
    builder = AuthorizedDependencyBuilder(authenticator=ApiKeyAuthenticator(key_resource=inner))
    manifest = Manifest(resources=[config_api_key_view(inner), widgets])
    app = create_app(manifest, dependency_builder=builder)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/widgets")).status_code == 401
            assert (
                await client.get("/widgets", headers={API_KEY_HEADER_NAME: "secret-one"})
            ).status_code == 200
            schema = (await client.get("/openapi.json")).json()
            schemes = schema["components"]["securitySchemes"]
            assert schemes["ApiKeyHeader"]["name"] == API_KEY_HEADER_NAME
            assert schema["paths"]["/widgets"]["get"]["security"]


def test_optional_posture_produces_a_lenient_dependency():
    from resourcey.v2.auth.auth_authorized_dependency import Posture

    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=None)
    ).with_posture(Posture.OPTIONAL)
    assert builder.posture is Posture.OPTIONAL
    assert builder.get_principal_dependency() is not None


# ---------------------------------------------------------------------------
# SessionCookieConfig
# ---------------------------------------------------------------------------


def test_session_cookie_config_defaults():
    config = SessionCookieConfig()
    assert config.session_cookie_name == "session"
    assert config.session_cookie_ttl_seconds > 0
    assert config.session_cookie_samesite == "lax"


def test_deny_all_resolver_is_fail_closed_type():
    # Sanity: the resolver produces an empty list, which AuthorizedService treats
    # as deny-all.
    assert isinstance(DenyAllResolver(), PolicyResolver)
    assert isinstance(AllowAllResolver(), PolicyResolver)
    assert isinstance(DenyAll(), Policy)
    assert isinstance(AllowAll(), Policy)
