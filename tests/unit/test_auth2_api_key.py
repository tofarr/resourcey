"""Tests for the auth2 environment API-key posture (issue #63).

Covers the three surfaces :class:`ApiKeyDependencyBuilder` offers:

* the general-purpose ``api_key_dependency`` used by routers / endpoints,
* the :meth:`ApiKeyDependencyBuilder.get_service_dependency` builder seam,
* config resolution via ``DEPENDENCY_BUILDER_CLASS`` + the key env vars.

Plus an end-to-end run through ``ResourceManifest.create_app`` with the builder
selected from the environment, so the posture is exercised the way a deployment
uses it.

Dependencies are attached as default-argument ``Depends(...)`` rather than
``Annotated`` so this module's ``from __future__ import annotations`` does not
turn them into strings FastAPI must resolve against module globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app_resources import AppWidget
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.app_context import AppContext
from resourcey.auth2.auth2_api_key import (
    API_KEY_HEADER_NAME,
    ApiKeyDependencyBuilder,
    get_api_key_dependency,
)
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import clear_config_cache, set_config
from resourcey.manifest import ResourceManifest
from resourcey.resource.base import BaseResource
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.sql import _SESSION_FACTORY_KEY, ResourceyBase

_SENTINEL: Any = object()


@pytest.fixture(autouse=True)
def _reset_config():
    """Clear runtime + instance config caches around every test."""
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()
    yield
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()


class _RecordingResource(BaseResource):
    """A resource whose service dependency records whether storage was opened."""

    opened = False

    async def get_service_dependency(self, request: Request) -> AsyncIterator[Any]:
        type(self).opened = True
        yield _SENTINEL


def _app_with(builder: ApiKeyDependencyBuilder) -> FastAPI:
    """An app whose one endpoint requires ``builder``'s API key."""
    app = FastAPI()

    async def secure(_: None = Depends(builder.api_key_dependency)) -> dict[str, bool]:
        return {"ok": True}

    app.get("/secure")(secure)
    return app


def _service_app(dep: Any) -> FastAPI:
    """An app whose one endpoint receives the service from ``dep``."""
    app = FastAPI()

    async def svc(service: Any = Depends(dep)) -> dict[str, bool]:  # noqa: B008
        return {"sentinel": service is _SENTINEL}

    app.get("/svc")(svc)
    return app


@pytest.fixture
def builder() -> ApiKeyDependencyBuilder:
    return ApiKeyDependencyBuilder(api_keys=["alpha", "beta"])


@pytest_asyncio.fixture
async def client(builder: ApiKeyDependencyBuilder) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_app_with(builder))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestApiKeyDependency:
    @pytest.mark.asyncio
    async def test_valid_key_via_x_api_key(self, client: AsyncClient) -> None:
        resp = await client.get("/secure", headers={API_KEY_HEADER_NAME: "alpha"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_either_configured_key_is_accepted(self, client: AsyncClient) -> None:
        """Two keys so rotation can overlap (old + new both valid)."""
        for key in ("alpha", "beta"):
            resp = await client.get("/secure", headers={API_KEY_HEADER_NAME: key})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_key_via_bearer(self, client: AsyncClient) -> None:
        resp = await client.get("/secure", headers={"Authorization": "Bearer beta"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_key_is_401(self, client: AsyncClient) -> None:
        resp = await client.get("/secure")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_is_401(self, client: AsyncClient) -> None:
        resp = await client.get("/secure", headers={API_KEY_HEADER_NAME: "wrong"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_present_but_invalid_header_denies_despite_valid_bearer(
        self, client: AsyncClient
    ) -> None:
        """The X-API-Key header takes precedence over the bearer fallback."""
        resp = await client.get(
            "/secure",
            headers={API_KEY_HEADER_NAME: "wrong", "Authorization": "Bearer beta"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_key_list_fails_closed(self) -> None:
        """No configured keys denies every request rather than opening the API."""
        transport = ASGITransport(app=_app_with(ApiKeyDependencyBuilder()))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/secure", headers={API_KEY_HEADER_NAME: "anything"})
        assert resp.status_code == 401

    def test_openapi_declares_both_schemes(self) -> None:
        schemes = _app_with(ApiKeyDependencyBuilder(api_keys=["alpha"])).openapi()["components"][
            "securitySchemes"
        ]
        assert set(schemes) == {"ApiKeyHeader", "ApiKeyBearer"}
        assert schemes["ApiKeyHeader"]["name"] == API_KEY_HEADER_NAME

    def test_is_valid_api_key_handles_missing_and_empty(self) -> None:
        builder = ApiKeyDependencyBuilder(api_keys=[""])
        assert builder.is_valid_api_key(None) is False
        assert builder.is_valid_api_key("") is False


class TestServiceDependency:
    @pytest.mark.asyncio
    async def test_denies_before_opening_storage(self) -> None:
        """An unauthenticated request is rejected before storage is opened."""
        resource = _RecordingResource()
        _RecordingResource.opened = False
        dep = ApiKeyDependencyBuilder(api_keys=["alpha"]).get_service_dependency(resource)
        transport = ASGITransport(app=_service_app(dep))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/svc", headers={API_KEY_HEADER_NAME: "wrong"})
        assert resp.status_code == 401
        assert _RecordingResource.opened is False

    @pytest.mark.asyncio
    async def test_yields_resource_service_when_authenticated(self) -> None:
        resource = _RecordingResource()
        _RecordingResource.opened = False
        dep = ApiKeyDependencyBuilder(api_keys=["alpha"]).get_service_dependency(resource)
        transport = ASGITransport(app=_service_app(dep))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/svc", headers={API_KEY_HEADER_NAME: "alpha"})
        assert resp.status_code == 200
        assert resp.json() == {"sentinel": True}
        assert _RecordingResource.opened is True


class TestConfigResolution:
    def test_builder_resolved_from_env_indexed_keys(self, monkeypatch) -> None:
        """LazyField loads ApiKeyDependencyBuilder and its indexed key list."""
        monkeypatch.setenv(
            "DEPENDENCY_BUILDER_CLASS",
            "resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder",
        )
        monkeypatch.setenv("DEPENDENCY_BUILDER_API_KEYS_0", "key-one")
        monkeypatch.setenv("DEPENDENCY_BUILDER_API_KEYS_1", "key-two")
        builder = FrameworkConfig().dependency_builder
        assert isinstance(builder, ApiKeyDependencyBuilder)
        assert [k.get_secret_value() for k in builder.api_keys] == ["key-one", "key-two"]

    def test_builder_resolved_from_env_json_keys(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "DEPENDENCY_BUILDER_CLASS",
            "resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder",
        )
        monkeypatch.setenv("DEPENDENCY_BUILDER_API_KEYS", '["json-key"]')
        builder = FrameworkConfig().dependency_builder
        assert isinstance(builder, ApiKeyDependencyBuilder)
        assert [k.get_secret_value() for k in builder.api_keys] == ["json-key"]

    def test_get_api_key_dependency_returns_bound_method(self) -> None:
        builder = ApiKeyDependencyBuilder(api_keys=["alpha"])
        cfg = FrameworkConfig()
        cfg.__dict__["_lazy_dependency_builder"] = builder
        set_config(cfg)
        try:
            assert get_api_key_dependency() == builder.api_key_dependency
        finally:
            clear_config_cache()

    def test_get_api_key_dependency_rejects_other_builder(self) -> None:
        from resourcey.config.config_dependency import DefaultDependencyBuilder

        cfg = FrameworkConfig()
        cfg.__dict__["_lazy_dependency_builder"] = DefaultDependencyBuilder()
        set_config(cfg)
        try:
            with pytest.raises(ResourceyConfigError, match="not ApiKeyDependencyBuilder"):
                get_api_key_dependency()
        finally:
            clear_config_cache()


@pytest_asyncio.fixture
async def secured_app(monkeypatch) -> AsyncIterator[FastAPI]:
    """A manifest app secured by the env-configured API key builder."""
    monkeypatch.setenv(
        "DEPENDENCY_BUILDER_CLASS",
        "resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder",
    )
    monkeypatch.setenv("DEPENDENCY_BUILDER_API_KEYS", '["secret-key"]')

    AppWidget().get_sql_alchemy_model()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    cfg = FrameworkConfig()
    ctx = AppContext(cfg)
    ctx.set(_SESSION_FACTORY_KEY, factory)
    manifest = ResourceManifest(resources=(AppWidget,))
    app = manifest.create_app(config=cfg, app_context=ctx)
    async with manifest:
        yield app
    await engine.dispose()


class TestManifestIntegration:
    @pytest.mark.asyncio
    async def test_routes_require_api_key(self, secured_app: FastAPI) -> None:
        transport = ASGITransport(app=secured_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            denied = await c.get("/app-widgets")
            assert denied.status_code == 401

            allowed = await c.get("/app-widgets", headers={API_KEY_HEADER_NAME: "secret-key"})
            assert allowed.status_code == 200
            assert allowed.json()["items"] == []

    @pytest.mark.asyncio
    async def test_authenticated_create_round_trips(self, secured_app: FastAPI) -> None:
        transport = ASGITransport(app=secured_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/app-widgets",
                json={"id": 1, "label": "hello"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert created.status_code == 201
            assert created.json()["label"] == "hello"
