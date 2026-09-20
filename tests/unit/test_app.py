"""Tests for ``resourcey.app.create_app`` (issue #21).

Covers:
- ``create_app`` builds a FastAPI with routes for each registered resource,
  error handlers, CORS middleware, and engine/session lifecycle.
- The explicit ``resources=`` arg wins over ``config.resources``.
- Config-driven ``resources`` (dotted import paths) resolve to classes.
- The ``config=`` escape hatch installs the instance via ``set_config``.
- The engine / session-factory escape hatches are honoured.
- The lifespan disposes an engine the app built, and leaves a caller-supplied
  one alone.
- Config is never read at import time (importing ``resourcey.app`` without env
  set does not build config).
- An app subclass via ``RESOURCEY_CONFIG_CLASS`` is picked up end-to-end.
- HTTP-level CRUD works against the assembled app (httpx ASGI transport).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from app_resources import AppGadget, AppWidget
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.app import create_app
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import clear_config_cache, get_config, set_config
from resourcey.resource.errors import NotFoundError, ResourceyConfigError
from resourcey.resource.sql import ResourceyBase, SqlResource

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


# ---------------------------------------------------------------------------
# Test config subclass (module-level so RESOURCEY_CONFIG_CLASS can import it)
# ---------------------------------------------------------------------------


class _AppConfig(FrameworkConfig):
    """A FrameworkConfig subclass exercising the config-class escape hatch."""

    @classmethod
    def get_prefix(cls) -> str:
        return "RESOURCEY"


# ---------------------------------------------------------------------------
# Shared SQLite engine + factory (StaticPool so cross-request state persists)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_factory() -> async_sessionmaker[AsyncSession]:
    """In-memory SQLite factory with resource tables created."""
    for _r in (AppWidget, AppGadget):
        _r.get_sql_alchemy_model()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def app_with_resources(sqlite_factory) -> FastAPI:
    """An assembled app serving AppWidget + AppGadget via explicit resources."""
    return create_app(resources=[AppWidget, AppGadget], session_factory=sqlite_factory)


@pytest_asyncio.fixture
async def client(app_with_resources) -> AsyncClient:
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
    """Build an httpx client against an app (caller closes via context)."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _paths(app: FastAPI) -> set[str]:
    """Collect served paths by issuing a no-body request to each candidate.

    Robust against FastAPI's internal route representation (``_IncludedRouter``
    in newer versions hides flattened paths). A path "exists" if a request to
    it does not return 404.
    """
    candidates = {"/app-widgets", "/app-gadgets", "/app-widgets/batch-read"}
    found: set[str] = set()
    async with await _client_for(app) as c:
        for path in candidates:
            resp = await c.get(path)
            if resp.status_code != 404:
                found.add(path)
    return found


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


class TestCreateAppStructure:
    @pytest.mark.asyncio
    async def test_routes_registered_for_each_resource(self, app_with_resources):
        paths = await _paths(app_with_resources)
        assert "/app-widgets" in paths
        assert "/app-gadgets" in paths

    @pytest.mark.asyncio
    async def test_seven_actions_registered(self, client):
        # POST/GET/PATCH/DELETE + batch-read all respond (not 404).
        assert (await client.get("/app-widgets")).status_code != 404
        assert (await client.post("/app-widgets", json={})).status_code != 404
        assert (await client.get("/app-widgets/batch-read")).status_code != 404
        # {id} route: a GET on a non-existent id yields 404 (not_found), which
        # proves the route exists (an absent route yields 404 from FastAPI's
        # default handler with a plain JSON body, not our error envelope).
        resp = await client.get("/app-widgets/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_no_resource_routes_when_empty(self, sqlite_factory):
        app = create_app(resources=[], session_factory=sqlite_factory)
        async with await _client_for(app) as c:
            assert (await c.get("/app-widgets")).status_code == 404

    def test_error_handlers_registered(self, app_with_resources):
        assert NotFoundError in app_with_resources.exception_handlers

    def test_cors_middleware_added_when_origins_configured(self, sqlite_factory):
        set_config(FrameworkConfig().model_copy(update={"cors_origins": ["https://example.com"]}))
        app = create_app(resources=[], session_factory=sqlite_factory)
        assert _cors_middleware_present(app)

    def test_no_cors_middleware_when_origins_empty(self, sqlite_factory):
        set_config(FrameworkConfig())
        app = create_app(resources=[], session_factory=sqlite_factory)
        assert not _cors_middleware_present(app)

    def test_wildcard_origin_disables_credentials(self, sqlite_factory):
        # The CORS spec forbids credentials with a wildcard origin; browsers
        # would otherwise silently reject credentialed responses.
        set_config(FrameworkConfig().model_copy(update={"cors_origins": ["*"]}))
        app = create_app(resources=[], session_factory=sqlite_factory)
        mw = next(
            m for m in app.user_middleware if getattr(m.cls, "__name__", "") == "CORSMiddleware"
        )
        assert mw.kwargs["allow_origins"] == ["*"]
        assert mw.kwargs["allow_credentials"] is False


# ---------------------------------------------------------------------------
# Config-driven resources resolution
# ---------------------------------------------------------------------------


class TestConfigDrivenResources:
    @pytest.mark.asyncio
    async def test_resources_read_from_config(self, sqlite_factory, monkeypatch):
        monkeypatch.setenv(
            "RESOURCEY_RESOURCES",
            f'["{AppWidget.__module__}.AppWidget","{AppGadget.__module__}.AppGadget"]',
        )
        FrameworkConfig.clear_instance_cache()
        app = create_app(session_factory=sqlite_factory)
        paths = await _paths(app)
        assert "/app-widgets" in paths
        assert "/app-gadgets" in paths

    @pytest.mark.asyncio
    async def test_resources_empty_when_unset(self, sqlite_factory, monkeypatch):
        monkeypatch.delenv("RESOURCEY_RESOURCES", raising=False)
        FrameworkConfig.clear_instance_cache()
        app = create_app(session_factory=sqlite_factory)
        paths = await _paths(app)
        assert "/app-widgets" not in paths

    @pytest.mark.asyncio
    async def test_resources_sequential_env_form(self, sqlite_factory, monkeypatch):
        monkeypatch.delenv("RESOURCEY_RESOURCES", raising=False)
        monkeypatch.setenv("RESOURCEY_RESOURCES_0", f"{AppWidget.__module__}.AppWidget")
        monkeypatch.setenv("RESOURCEY_RESOURCES_1", f"{AppGadget.__module__}.AppGadget")
        FrameworkConfig.clear_instance_cache()
        app = create_app(session_factory=sqlite_factory)
        paths = await _paths(app)
        assert "/app-widgets" in paths
        assert "/app-gadgets" in paths

    @pytest.mark.asyncio
    async def test_explicit_resources_win_over_config(self, sqlite_factory, monkeypatch):
        monkeypatch.setenv("RESOURCEY_RESOURCES", f'["{AppGadget.__module__}.AppGadget"]')
        FrameworkConfig.clear_instance_cache()
        app = create_app(resources=[AppWidget], session_factory=sqlite_factory)
        paths = await _paths(app)
        assert "/app-widgets" in paths
        assert "/app-gadgets" not in paths

    def test_invalid_resource_path_raises_config_error(self, sqlite_factory, monkeypatch):
        monkeypatch.setenv("RESOURCEY_RESOURCES", '["nonexistent.module.Nope"]')
        FrameworkConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="resources"):
            create_app(session_factory=sqlite_factory)

    def test_non_base_resource_path_raises_config_error(self, sqlite_factory, monkeypatch):
        # datetime is importable but not a BaseResource subclass.
        monkeypatch.setenv("RESOURCEY_RESOURCES", '["datetime.datetime"]')
        FrameworkConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError):
            create_app(session_factory=sqlite_factory)


# ---------------------------------------------------------------------------
# Config escape hatches
# ---------------------------------------------------------------------------


class TestConfigEscapeHatches:
    def test_config_arg_installed_as_active(self, sqlite_factory):
        cfg = FrameworkConfig(host="1.2.3.4", port=9999)
        create_app(config=cfg, resources=[], session_factory=sqlite_factory)
        assert get_config() is cfg

    def test_config_class_env_var_picked_up(self, sqlite_factory, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{_AppConfig.__module__}._AppConfig")
        create_app(resources=[], session_factory=sqlite_factory)
        assert isinstance(get_config(), _AppConfig)


# ---------------------------------------------------------------------------
# Engine / session-factory escape hatches + lifespan
# ---------------------------------------------------------------------------


class TestEngineEscapeHatches:
    @pytest.mark.asyncio
    async def test_app_context_pre_seeded_factory_reused(self):
        # The app_context escape hatch: a caller pre-seeds the SQL session
        # factory on the context so SqlResource.lifespan skips building an
        # engine. Replaces the old engine= arg.
        from resourcey.app_context import AppContext
        from resourcey.resource.sql import _SESSION_FACTORY_KEY

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, factory)
        SqlResource._session_factory = factory
        app = create_app(resources=[], app_context=ctx)
        assert app.router.lifespan_context is not None
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_app_built_engine_disposed_on_lifespan_exit(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_RESOURCES", raising=False)
        FrameworkConfig.clear_instance_cache()
        app = create_app(resources=[])
        # The lifespan owns the engine; entering/exiting should dispose it
        # without error (the database_url points at postgres+asyncpg by
        # default, but dispose() on a never-used engine is a no-op).
        async with app.router.lifespan_context(app):
            pass

    @pytest.mark.asyncio
    async def test_caller_session_factory_not_disposed(self, sqlite_factory):
        # When only a session_factory is supplied, the app does not own an
        # engine and must not dispose the caller's. Exiting the lifespan must
        # not break the factory (it stays usable).
        app = create_app(resources=[], session_factory=sqlite_factory)
        async with app.router.lifespan_context(app):
            pass
        async with sqlite_factory() as session:
            assert session is not None


class TestResourceLifespan:
    """Tests for the resource-level lifespan protocol (issue #49)."""

    @pytest.mark.asyncio
    async def test_sql_lifespan_builds_session_factory_from_config(self, monkeypatch):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig

        monkeypatch.setenv("RESOURCEY_DATABASE_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig()
        ctx = AppContext(cfg)
        assert SqlResource._session_factory is None
        async with SqlResource.lifespan(ctx):
            factory = SqlResource.get_session_factory()
            assert isinstance(factory, async_sessionmaker)
        # The lifespan registers a clearer on ctx; create_app calls aclose.
        await ctx.aclose()
        assert SqlResource._session_factory is None

    @pytest.mark.asyncio
    async def test_sql_lifespan_reuses_pre_seeded_factory(self, sqlite_factory):
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig
        from resourcey.resource.sql import _SESSION_FACTORY_KEY

        ctx = AppContext(FrameworkConfig())
        ctx.set(_SESSION_FACTORY_KEY, sqlite_factory)
        SqlResource._session_factory = sqlite_factory
        async with SqlResource.lifespan(ctx):
            assert SqlResource.get_session_factory() is sqlite_factory
        await ctx.aclose()
        assert SqlResource._session_factory is None

    @pytest.mark.asyncio
    async def test_sql_build_session_factory_returns_disposer(self):
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig

        ctx = AppContext(FrameworkConfig())
        factory, dispose = SqlResource.build_session_factory(ctx)
        assert factory is not None
        await dispose()

    @pytest.mark.asyncio
    async def test_base_resource_lifespan_is_noop(self):
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig
        from resourcey.resource.base import BaseResource

        ctx = AppContext(FrameworkConfig())
        # The base lifespan is a no-op yield — entering/exiting must not raise.
        async with BaseResource.lifespan(ctx):
            pass


# ---------------------------------------------------------------------------
# Import-time safety
# ---------------------------------------------------------------------------


class TestImportTimeSafety:
    def test_importing_app_does_not_read_config(self, monkeypatch):
        # With no env vars set, importing resourcey.app must not build config.
        for key in ("RESOURCEY_RESOURCES", "RESOURCEY_HOST", _CONFIG_CLASS_ENV):
            monkeypatch.delenv(key, raising=False)
        import importlib

        clear_config_cache()
        importlib.import_module("resourcey.app")
        from resourcey.config import config_runtime

        assert config_runtime._env_resolved_instance is None
        assert config_runtime._config_override is None


# ---------------------------------------------------------------------------
# HTTP-level integration
# ---------------------------------------------------------------------------


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
        # Unknown sort field -> 422 (sort is now an enum validated by FastAPI,
        # consistent with typed filter params #31).
        resp = await client.get("/app-widgets", params={"sort": "nonsense"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# clear_config_cache helper
# ---------------------------------------------------------------------------


class TestClearConfigCache:
    def test_clears_override(self):
        cfg = FrameworkConfig()
        set_config(cfg)
        assert get_config() is cfg
        clear_config_cache()
        assert get_config() is not cfg
