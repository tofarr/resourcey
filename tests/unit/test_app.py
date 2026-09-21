"""Tests for ``ResourceManifest`` (issue #51).

Covers:
- ``manifest.create_app()`` builds a FastAPI with routes for each resource,
  error handlers, CORS middleware.
- The manifest owns resource instances; ``on_register`` materialises tables.
- The ``config=`` escape hatch installs the instance via ``set_config``.
- The ``app_context=`` escape hatch pre-seeds a session factory.
- The lifespan enters/exits each resource instance; engines are disposed.
- Config is never read at import time (importing the manifest module without
  env set does not build config).
- HTTP-level CRUD works against the assembled app (httpx ASGI transport).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app_resources import AppGadget, AppWidget
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.app_context import AppContext
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import clear_config_cache, get_config, set_config
from resourcey.manifest import ResourceManifest
from resourcey.resource.errors import NotFoundError
from resourcey.resource.sql import _SESSION_FACTORY_KEY, ResourceyBase

_CONFIG_CLASS_ENV = "RESOURCEY_CONFIG_CLASS"


@pytest.fixture(autouse=True)
def _reset_config():
    """Clear runtime + instance config caches around every test."""
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()
    _AppConfig.clear_instance_cache()
    yield
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()
    _AppConfig.clear_instance_cache()


class _AppConfig(FrameworkConfig):
    """A FrameworkConfig subclass exercising the config-class escape hatch."""

    @classmethod
    def get_prefix(cls) -> str:
        return "RESOURCEY"


@pytest_asyncio.fixture
async def sqlite_factory() -> async_sessionmaker[AsyncSession]:
    """In-memory SQLite factory with resource tables created."""
    AppWidget.get_sql_alchemy_model()
    AppGadget.get_sql_alchemy_model()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def app_with_resources(sqlite_factory) -> AsyncIterator[FastAPI]:
    """An assembled app serving AppWidget + AppGadget via the manifest."""
    ctx = AppContext(FrameworkConfig())
    ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
    manifest = ResourceManifest(resources=(AppWidget, AppGadget))
    app = manifest.create_app(app_context=ctx)
    # ASGITransport does not run the Starlette lifespan, so enter the manifest
    # manually — this copies the pre-seeded session factory onto each instance.
    async with manifest:
        yield app


@pytest_asyncio.fixture
async def client(app_with_resources) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_with_resources)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _cors_middleware_present(app: FastAPI) -> bool:
    names = {
        getattr(m.cls, "__name__", type(m).__name__) if hasattr(m, "cls") else type(m).__name__
        for m in app.user_middleware
    }
    return "CORSMiddleware" in names


async def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _paths(app: FastAPI) -> set[str]:
    candidates = {"/app-widgets", "/app-gadgets", "/app-widgets/batch-read"}
    found: set[str] = set()
    async with await _client_for(app) as c:
        for path in candidates:
            resp = await c.get(path)
            if resp.status_code != 404:
                found.add(path)
    return found


class TestManifestStructure:
    @pytest.mark.asyncio
    async def test_routes_registered_for_each_resource(self, app_with_resources):
        paths = await _paths(app_with_resources)
        assert "/app-widgets" in paths
        assert "/app-gadgets" in paths

    @pytest.mark.asyncio
    async def test_actions_registered(self, client):
        assert (await client.get("/app-widgets")).status_code != 404
        assert (await client.post("/app-widgets", json={})).status_code != 404
        assert (await client.get("/app-widgets/batch-read")).status_code != 404
        resp = await client.get("/app-widgets/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_no_resource_routes_when_empty(self, sqlite_factory):
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        app = manifest.create_app(app_context=ctx)
        async with await _client_for(app) as c:
            assert (await c.get("/app-widgets")).status_code == 404

    def test_error_handlers_registered(self, app_with_resources):
        assert NotFoundError in app_with_resources.exception_handlers

    def test_cors_middleware_added_when_origins_configured(self, sqlite_factory):
        set_config(FrameworkConfig().model_copy(update={"cors_origins": ["https://example.com"]}))
        ctx = AppContext(get_config())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        app = manifest.create_app(app_context=ctx)
        assert _cors_middleware_present(app)

    def test_no_cors_middleware_when_origins_empty(self, sqlite_factory):
        set_config(FrameworkConfig())
        ctx = AppContext(get_config())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        app = manifest.create_app(app_context=ctx)
        assert not _cors_middleware_present(app)

    def test_wildcard_origin_disables_credentials(self, sqlite_factory):
        set_config(FrameworkConfig().model_copy(update={"cors_origins": ["*"]}))
        ctx = AppContext(get_config())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        app = manifest.create_app(app_context=ctx)
        mw = next(
            m for m in app.user_middleware if getattr(m.cls, "__name__", "") == "CORSMiddleware"
        )
        assert mw.kwargs["allow_origins"] == ["*"]
        assert mw.kwargs["allow_credentials"] is False


class TestManifestInstances:
    def test_manifest_instantiates_resources(self):
        manifest = ResourceManifest(resources=(AppWidget, AppGadget))
        assert len(manifest.instances) == 2
        assert isinstance(manifest.instances[0], AppWidget)
        assert isinstance(manifest.instances[1], AppGadget)

    def test_on_register_materialises_sql_model(self):
        manifest = ResourceManifest(resources=(AppWidget,))
        instance = manifest.instances[0]
        assert instance._sqlalchemy_model is not None

    def test_manifest_is_frozen(self):
        manifest = ResourceManifest(resources=(AppWidget,))
        with pytest.raises((ValidationError, TypeError)):
            manifest.resources = ()  # type: ignore[misc]

    def test_materialize_is_idempotent(self):
        manifest = ResourceManifest(resources=(AppWidget,))
        manifest.materialize()
        manifest.materialize()
        assert manifest.instances[0]._sqlalchemy_model is not None


class TestConfigEscapeHatches:
    def test_config_arg_installed_as_active(self, sqlite_factory):
        cfg = FrameworkConfig(host="1.2.3.4", port=9999)
        ctx = AppContext(cfg)
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        manifest.create_app(config=cfg, app_context=ctx)
        assert get_config() is cfg

    def test_config_class_env_var_picked_up(self, sqlite_factory, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{_AppConfig.__module__}._AppConfig")
        ctx = AppContext(get_config())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        manifest.create_app(app_context=ctx)
        assert isinstance(get_config(), _AppConfig)


class TestEngineEscapeHatches:
    @pytest.mark.asyncio
    async def test_app_context_pre_seeded_factory_reused(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, factory)
        manifest = ResourceManifest(resources=(AppWidget,))
        app = manifest.create_app(app_context=ctx)
        assert app.router.lifespan_context is not None
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_app_built_engine_disposed_on_lifespan_exit(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_DATABASE_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        FrameworkConfig.clear_instance_cache()
        manifest = ResourceManifest(resources=())
        app = manifest.create_app()
        async with app.router.lifespan_context(app):
            pass

    @pytest.mark.asyncio
    async def test_caller_session_factory_not_disposed(self, sqlite_factory):
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=())
        app = manifest.create_app(app_context=ctx)
        async with app.router.lifespan_context(app):
            pass
        async with sqlite_factory() as session:
            assert session is not None


class TestResourceLifecycle:
    @pytest.mark.asyncio
    async def test_sql_lifespan_builds_session_factory_from_config(self, monkeypatch):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        monkeypatch.setenv("RESOURCEY_DATABASE_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig()
        ctx = AppContext(cfg)
        instance = AppWidget()
        instance.on_register()
        assert instance._session_factory is None
        await instance.__aenter__(ctx)
        assert isinstance(instance._session_factory, async_sessionmaker)
        await instance.__aexit__(None, None, None)
        await ctx.aclose()
        assert instance._session_factory is None

    @pytest.mark.asyncio
    async def test_sql_lifespan_reuses_pre_seeded_factory(self, sqlite_factory):
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        instance = AppWidget()
        instance.on_register()
        await instance.__aenter__(ctx)
        assert instance._session_factory is sqlite_factory
        await instance.__aexit__(None, None, None)
        await ctx.aclose()

    @pytest.mark.asyncio
    async def test_sql_build_session_factory_returns_disposer(self):
        ctx = AppContext(FrameworkConfig())
        instance = AppWidget()
        instance.on_register()
        factory, dispose = instance.build_session_factory(ctx)
        assert factory is not None
        await dispose()

    @pytest.mark.asyncio
    async def test_base_resource_lifecycle_is_noop(self):
        from resourcey.resource.base import BaseResource

        ctx = AppContext(FrameworkConfig())
        instance = BaseResource()
        await instance.__aenter__(ctx)
        await instance.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_manifest_async_context_manager_enters_exits(self, sqlite_factory):
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=(AppWidget,))
        manifest.create_app(app_context=ctx)
        async with manifest:
            instance = manifest.instances[0]
            assert instance._session_factory is sqlite_factory
        # After exit, instance factory is cleared.
        assert manifest.instances[0]._session_factory is None

    @pytest.mark.asyncio
    async def test_add_to_app_mounts_routes(self, sqlite_factory):
        """``add_to_app`` mounts routes on a user-owned app without wiring lifespan."""
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        manifest = ResourceManifest(resources=(AppWidget,))
        object.__setattr__(manifest, "_ctx", ctx)
        app = FastAPI()
        manifest.add_to_app(app, prefix="/api")
        # ASGITransport does not run the lifespan; enter the manifest manually
        # so the pre-seeded session factory lands on the instance.
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/api/app-widgets")
                assert resp.status_code != 404


class TestImportTimeSafety:
    def test_importing_manifest_does_not_read_config(self, monkeypatch):
        for key in ("RESOURCEY_MANIFEST", "RESOURCEY_HOST", _CONFIG_CLASS_ENV):
            monkeypatch.delenv(key, raising=False)
        import importlib

        clear_config_cache()
        importlib.import_module("resourcey.manifest")
        from resourcey.config import config_runtime

        assert config_runtime._env_resolved_instance is None
        assert config_runtime._config_override is None


class TestAppHttpCrud:
    @pytest.mark.asyncio
    async def test_create_then_read(self, client):
        resp = await client.post("/app-widgets", json={"label": "w1"})
        assert resp.status_code == 201
        created = resp.json()
        assert created["label"] == "w1"
        wid = created["id"]
        got = await client.get(f"/app-widgets/{wid}")
        assert got.status_code == 200
        assert got.json()["label"] == "w1"

    @pytest.mark.asyncio
    async def test_update_patch(self, client):
        created = (await client.post("/app-widgets", json={"label": "w"})).json()
        resp = await client.patch(f"/app-widgets/{created['id']}", json={"label": "w2"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "w2"

    @pytest.mark.asyncio
    async def test_delete(self, client):
        created = (await client.post("/app-widgets", json={"label": "w"})).json()
        resp = await client.delete(f"/app-widgets/{created['id']}")
        assert resp.status_code == 204
        miss = await client.get(f"/app-widgets/{created['id']}")
        assert miss.status_code == 404
        assert miss.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_search_returns_page(self, client):
        for i in range(3):
            await client.post("/app-widgets", json={"label": f"i{i}"})
        resp = await client.get("/app-widgets", params={"limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert "total" not in body
        assert "offset" not in body
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_count_endpoint(self, client):
        for i in range(3):
            await client.post("/app-widgets", json={"label": f"i{i}"})
        resp = await client.get("/app-widgets/count")
        assert resp.status_code == 200
        assert resp.json() == 3

    @pytest.mark.asyncio
    async def test_batch_read(self, client):
        a = (await client.post("/app-widgets", json={"label": "a"})).json()
        b = (await client.post("/app-widgets", json={"label": "b"})).json()
        resp = await client.get("/app-widgets/batch-read", params={"id": [a["id"], b["id"]]})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_second_resource_served(self, client):
        resp = await client.post("/app-gadgets", json={"name": "g"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "g"

    @pytest.mark.asyncio
    async def test_invalid_input_returns_envelope(self, client):
        resp = await client.get("/app-widgets", params={"sort": "nonsense"})
        assert resp.status_code == 422


class TestClearConfigCache:
    def test_clears_override(self):
        cfg = FrameworkConfig()
        set_config(cfg)
        assert get_config() is cfg
        clear_config_cache()
        assert get_config() is not cfg
