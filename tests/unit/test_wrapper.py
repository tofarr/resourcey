"""Tests for ``WrapperResourceBase`` and ``get_exposed_resource`` (issues #55, #62)."""

from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.config.config_dependency import DependencyBuilder
from resourcey.resource.base import BaseResource
from resourcey.resource.field import ResourceyField
from resourcey.resource.routes import register_error_handlers, register_routes
from resourcey.resource.service_base import Action
from resourcey.resource.sql import ResourceyBase, SqlResource
from resourcey.resource.wrapper import WrapperResourceBase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class WrapperWidget(SqlResource):
    id: int
    name: str
    secret: SecretStr = SecretStr("hidden")


class PublicWidget(WrapperResourceBase):
    """A wrapper that hides the ``secret`` field from the read model."""

    def get_read_model(self) -> type[BaseModel]:
        return self._project_read_model(exclude=frozenset({"secret"}))


class ExposedWidget(SqlResource):
    """An internal resource whose outside-world view is ``PublicWidget``."""

    id: int
    name: str
    secret: SecretStr = SecretStr("hidden")

    def get_exposed_resource(self) -> BaseResource | None:
        return PublicWidget(inner=self)


class InternalResource(SqlResource):
    """An internal-only resource (``get_exposed_resource() is None``)."""

    id: int
    name: str

    def get_exposed_resource(self) -> BaseResource | None:
        return None


class PublicFilterableWidget(WrapperResourceBase):
    """Hides ``secret`` from a filterable resource's read model + query surface."""

    def get_read_model(self) -> type[BaseModel]:
        return self._project_read_model(exclude=frozenset({"secret"}))


class UnreadableFieldWidget(SqlResource):
    """A resource with a field marked non-readable (not just wrapper-hidden)."""

    id: int
    name: str
    secret: Annotated[str, Field(default="hidden"), ResourceyField(readable=False)]


class ExposedFilterableWidget(SqlResource):
    id: int
    name: str
    secret: str = "hidden"

    def get_exposed_resource(self) -> BaseResource | None:
        return PublicFilterableWidget(inner=self)

    @classmethod
    def get_search_filter_type(cls):
        from resourcey.util.search_filter import BaseSearchFilter

        model = cls.get_sql_alchemy_model()

        class _Filter(BaseSearchFilter[model]):  # type: ignore[valid-type]
            name__eq: str | None = None
            secret__eq: str | None = None

        return _Filter


class NarrowedActions(WrapperResourceBase):
    """A wrapper that only exposes READ + SEARCH."""

    def get_supported_actions(self) -> frozenset:
        inner = self._inner.get_supported_actions()
        return inner & frozenset({Action.READ, Action.SEARCH})


# ---------------------------------------------------------------------------
# Wrapper delegation
# ---------------------------------------------------------------------------


class TestWrapperDelegation:
    def test_model_fields_delegates_to_inner(self):
        inner = WrapperWidget()
        wrapper = PublicWidget(inner=inner)
        assert wrapper.model_fields is inner.model_fields
        assert set(wrapper.model_fields) == {"id", "name", "secret"}

    def test_model_fields_setter_is_noop(self):
        """The setter exists defensively but is a no-op."""
        wrapper = PublicWidget(inner=WrapperWidget())
        original = wrapper.model_fields
        wrapper.model_fields = {"should": "be ignored"}
        assert wrapper.model_fields is original

    def test_get_id_field_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_id_field() == "id"

    def test_get_sortable_fields_excludes_projected_fields(self):
        """A wrapper that hides a field must not leave it sortable (issue #62).

        ``?sort=secret`` discloses the relative order of a field absent from
        the response body, so the query surface narrows with the read model.
        """
        wrapper = PublicFilterableWidget(inner=ExposedFilterableWidget())
        assert wrapper.get_sortable_fields() == ["id", "name"]
        assert "secret" in ExposedFilterableWidget().get_sortable_fields()

    def test_get_queryable_fields_defaults_to_all(self):
        wrapper = NarrowedActions(inner=WrapperWidget())
        assert wrapper.get_queryable_fields() == frozenset({"id", "name", "secret"})

    def test_get_queryable_fields_excludes_projected_fields(self):
        wrapper = PublicFilterableWidget(inner=ExposedFilterableWidget())
        assert wrapper.get_queryable_fields() == frozenset({"id", "name"})

    def test_get_queryable_fields_covers_unreadable_fields(self):
        """A field the *inner* resource marks unreadable is non-queryable too.

        ``get_queryable_fields`` derives from the read model, which already
        drops unreadable fields, so the wrapper need not repeat the exclusion.
        """
        wrapper = WrapperResourceBase(inner=UnreadableFieldWidget())
        assert wrapper.get_queryable_fields() == frozenset({"id", "name"})

    def test_queryable_fields_answers_before_read_model_requested(self):
        """Reading sortable fields first still sees the projection (order-free)."""
        wrapper = PublicFilterableWidget(inner=ExposedFilterableWidget())
        assert wrapper.get_sortable_fields() == ["id", "name"]
        assert "secret" not in wrapper.get_read_model().model_fields

    def test_narrowed_filter_cls_is_cached(self):
        """Repeated calls reuse the same narrowed filter class."""
        from resourcey.resource.wrapper import _narrow_filter_cls
        from resourcey.util.search_filter import BaseSearchFilter

        class _Filter(BaseSearchFilter[None]):  # type: ignore[valid-type]
            name__eq: str | None = None
            secret__eq: str | None = None

        first = _narrow_filter_cls(_Filter, frozenset({"id", "name"}))
        second = _narrow_filter_cls(_Filter, frozenset({"id", "name"}))
        assert first is second
        assert set(first.model_fields) == {"name__eq"}
        # A different queryable set is a distinct cache entry.
        wider = _narrow_filter_cls(_Filter, frozenset({"id", "name", "secret"}))
        assert wider is _Filter

    def test_narrowed_filter_cls_cache_is_per_class(self):
        """A subclass does not reuse a base's narrowed class (own fields kept)."""
        from resourcey.resource.wrapper import _narrow_filter_cls
        from resourcey.util.search_filter import BaseSearchFilter

        class _Base(BaseSearchFilter[None]):  # type: ignore[valid-type]
            shared__eq: str | None = None
            secret__eq: str | None = None

        class _Sub(_Base):
            own__eq: str | None = None

        queryable = frozenset({"shared", "own"})
        _narrow_filter_cls(_Base, queryable)
        narrowed = _narrow_filter_cls(_Sub, queryable)
        # The subclass's own field survives; the base's cache entry is not reused.
        assert set(narrowed.model_fields) == {"shared__eq", "own__eq"}

    def test_narrowed_filter_cls_preserves_overrides(self):
        """Narrowing keeps the concrete filter's methods (not just its fields)."""
        from resourcey.resource.wrapper import _narrow_filter_cls
        from resourcey.util.search_filter import BaseSearchFilter

        class _Filter(BaseSearchFilter[None]):  # type: ignore[valid-type]
            name__eq: str | None = None
            secret__eq: str | None = None

            def sql_condition(self):
                return "OVERRIDDEN"

        narrowed = _narrow_filter_cls(_Filter, frozenset({"name"}))
        assert set(narrowed.model_fields) == {"name__eq"}
        assert narrowed(name__eq="x").sql_condition() == "OVERRIDDEN"

    def test_narrowed_filter_cls_rejects_projected_fields(self):
        """The narrowed class does not accept a hidden field's filter param."""
        from resourcey.resource.wrapper import _narrow_filter_cls
        from resourcey.util.search_filter import BaseSearchFilter

        class _Filter(BaseSearchFilter[None]):  # type: ignore[valid-type]
            name__eq: str | None = None
            secret__eq: str | None = None

        narrowed = _narrow_filter_cls(_Filter, frozenset({"name"}))
        assert "secret__eq" not in narrowed.model_fields
        # The dropped field is not part of the model, so the param cannot apply.
        assert not hasattr(narrowed(name__eq="x"), "secret__eq")

    def test_get_search_filter_type_excludes_projected_fields(self):
        """A hidden field's filter param is dropped from the exposed filter class."""
        wrapper = PublicFilterableWidget(inner=ExposedFilterableWidget())
        fields = set(wrapper.get_search_filter_type().model_fields)
        assert fields == {"name__eq"}

    def test_get_search_filter_type_delegates_when_not_projected(self):
        wrapper = NarrowedActions(inner=ExposedFilterableWidget())
        filter_cls = wrapper.get_search_filter_type()
        assert set(filter_cls.model_fields) == {"name__eq", "secret__eq"}

    def test_actions_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.actions == WrapperWidget().actions

    def test_get_supported_actions_delegates_by_default(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_supported_actions() == WrapperWidget().get_supported_actions()

    def test_get_create_model_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_create_model() is WrapperWidget().get_create_model()

    def test_get_update_model_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_update_model() is WrapperWidget().get_update_model()

    def test_get_resource_path_uses_wrapper_class_name(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_resource_path() == "public-widgets"

    def test_get_exposed_resource_returns_self(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_exposed_resource() is wrapper

    def test_get_exposed_resource_is_not_delegated_to_inner(self):
        """A wrapper never delegates exposure to the inner resource.

        Delegating would return the *inner* resource (the default hook returns
        ``self``), discarding the wrapper's projection and re-exposing hidden
        fields — a data-leak hazard.
        """
        inner = ExposedWidget()
        wrapper = PublicWidget(inner=inner)
        assert wrapper.get_exposed_resource() is wrapper
        assert wrapper.get_exposed_resource() is not inner

    def test_get_orm_model_delegates(self):
        inner = WrapperWidget()
        wrapper = PublicWidget(inner=inner)
        assert wrapper.get_orm_model() is inner.get_orm_model()

    def test_on_register_delegates(self):
        inner = WrapperWidget()
        wrapper = PublicWidget(inner=inner)
        # No-op for SqlResource, but should not raise
        wrapper.on_register()

    def test_get_config_for_field_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        cfg = wrapper.get_config_for_field("name", WrapperWidget.model_fields["name"])
        expected = WrapperWidget().get_config_for_field("name", WrapperWidget.model_fields["name"])
        assert cfg == expected

    def test_get_cache_strategy_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_cache_strategy() is WrapperWidget().get_cache_strategy()

    def test_get_read_model_delegates_to_inner_when_not_overridden(self):
        """A wrapper that doesn't override get_read_model delegates to inner."""
        wrapper = NarrowedActions(inner=WrapperWidget())
        assert wrapper.get_read_model() is WrapperWidget().get_read_model()

    @pytest.mark.asyncio
    async def test_lifecycle_delegates(self):
        """__aenter__/__aexit__ delegate to the inner resource."""
        from unittest.mock import MagicMock

        inner = WrapperWidget()
        wrapper = PublicWidget(inner=inner)
        ctx = MagicMock()
        await wrapper.__aenter__(ctx)
        await wrapper.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Subtract-attributes read model
# ---------------------------------------------------------------------------


class TestWrapperSubtractReadModel:
    def test_excluded_field_absent_from_read_model(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        read_model = wrapper.get_read_model()
        assert "id" in read_model.model_fields
        assert "name" in read_model.model_fields
        assert "secret" not in read_model.model_fields

    def test_read_model_cached_on_instance(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        first = wrapper.get_read_model()
        second = wrapper.get_read_model()
        assert first is second

    def test_inner_read_model_still_has_secret(self):
        """The inner resource's read model is unaffected by the wrapper."""
        inner = WrapperWidget()
        inner_read = inner.get_read_model()
        assert "secret" in inner_read.model_fields

    def test_secret_serializer_not_attached_for_excluded(self):
        """Excluded SecretStr fields don't get serializers on the wrapper model."""
        wrapper = PublicWidget(inner=WrapperWidget())
        read_model = wrapper.get_read_model()
        # The wrapper's read model has no secret field, so no secret serializer
        # is attached. The inner's read model still has it.
        inner_read = WrapperWidget().get_read_model()
        assert "secret" in inner_read.model_fields
        assert "secret" not in read_model.model_fields


# ---------------------------------------------------------------------------
# Narrowed actions
# ---------------------------------------------------------------------------


class TestWrapperNarrowedActions:
    def test_narrowed_actions_subset_of_inner(self):
        wrapper = NarrowedActions(inner=WrapperWidget())
        actions = wrapper.get_supported_actions()
        assert Action.READ in actions
        assert Action.SEARCH in actions
        # CREATE, UPDATE, DELETE etc. are excluded
        assert Action.CREATE not in actions
        assert Action.UPDATE not in actions

    def test_narrowed_actions_is_frozenset(self):
        wrapper = NarrowedActions(inner=WrapperWidget())
        assert isinstance(wrapper.get_supported_actions(), (frozenset, set))


# ---------------------------------------------------------------------------
# get_exposed_resource
# ---------------------------------------------------------------------------


class TestGetExposedResource:
    def test_default_returns_self(self):
        resource = WrapperWidget()
        assert resource.get_exposed_resource() is resource

    def test_internal_resource_returns_none(self):
        assert InternalResource().get_exposed_resource() is None

    def test_resource_can_return_a_wrapper(self):
        resource = ExposedWidget()
        exposed = resource.get_exposed_resource()
        assert isinstance(exposed, PublicWidget)
        assert exposed._inner is resource


# ---------------------------------------------------------------------------
# Route builder honours exposure
# ---------------------------------------------------------------------------


class TestRouteBuilderExposure:
    def test_non_exposed_resource_registers_no_routes(self):
        """A resource whose get_exposed_resource() is None registers no routes."""
        app = FastAPI()
        router = register_routes(app, InternalResource())
        assert len(router.routes) == 0

    def test_exposed_resource_registers_routes(self):
        app = FastAPI()
        router = register_routes(app, WrapperWidget())
        assert len(router.routes) > 0

    def test_exposed_wrapper_registers_routes(self):
        app = FastAPI()
        router = register_routes(app, PublicWidget(inner=WrapperWidget()))
        assert len(router.routes) > 0

    def test_exposing_resource_serves_its_wrapper(self):
        """The exposed wrapper drives the route path (not the inner resource)."""
        app = FastAPI()
        router = register_routes(app, ExposedWidget())
        paths = {r.path for r in router.routes}
        assert "/public-widgets" in paths
        assert "/exposed-widgets" not in paths


# ---------------------------------------------------------------------------
# End-to-end: the exposed wrapper drives the service (issue #62, implication 1)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Shared in-memory SQLite engine + session factory for the HTTP tests."""
    # Resolve ORM models before create_all so the tables exist (mirrors the
    # eager resolution the service-level tests do at module scope).
    for resource_type in (
        ExposedWidget,
        WrapperWidget,
        InternalResource,
        ExposedFilterableWidget,
    ):
        resource_type().get_sql_alchemy_model()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


class TestExposedResourceBacksService:
    """Field hiding must apply to the response *body*, not just the schema.

    A raw ``JSONResponse`` bypasses FastAPI's ``response_model``, so if the
    service were built on the plain resource the hidden field would still be
    serialised. These tests pin the response body.
    """

    @pytest.mark.asyncio
    async def test_response_body_omits_hidden_field(self, session_factory) -> None:
        resource = ExposedWidget()
        resource.on_register()
        resource._session_factory = session_factory
        app = FastAPI()
        register_routes(app, resource)
        register_error_handlers(app)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            created = await client.post(
                "/public-widgets",
                json={"id": 1, "name": "n", "secret": "hunter2"},
            )
            assert created.status_code == 201
            assert created.json() == {"id": 1, "name": "n"}
            assert "secret" not in created.json()

            read = await client.get("/public-widgets/1")
            assert read.status_code == 200
            assert read.json() == {"id": 1, "name": "n"}
            assert "secret" not in read.json()

            listed = await client.get("/public-widgets")
            assert listed.status_code == 200
            assert listed.json()["items"] == [{"id": 1, "name": "n"}]

    @pytest.mark.filterwarnings("ignore::Warning")
    @pytest.mark.asyncio
    async def test_openapi_schema_matches_body(self, session_factory) -> None:
        """The exposed read model is what the OpenAPI schema advertises."""
        resource = ExposedWidget()
        resource.on_register()
        resource._session_factory = session_factory
        app = FastAPI()
        register_routes(app, resource)
        register_error_handlers(app)

        schema = app.openapi()
        props = schema["components"]["schemas"]["PublicWidgetRead"]["properties"]
        assert set(props) == {"id", "name"}

    @pytest.mark.asyncio
    async def test_internal_only_resource_serves_nothing(self) -> None:
        app = FastAPI()
        register_routes(app, InternalResource())
        paths = {r.path for r in app.routes}
        assert not any(p.startswith("/internal-resources") for p in paths)

    @pytest.mark.filterwarnings("ignore::Warning")
    @pytest.mark.asyncio
    async def test_hidden_field_not_sortable_over_http(self, session_factory) -> None:
        """``?sort=<hidden field>`` is rejected, not silently honoured (issue #62).

        Sorting by a hidden field discloses its relative order even though the
        field never appears in the response body.
        """
        resource = ExposedFilterableWidget()
        resource.on_register()
        resource._session_factory = session_factory
        app = FastAPI()
        register_routes(app, resource)
        register_error_handlers(app)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await client.post("/public-filterable-widgets", json={"id": 1, "name": "a"})
            await client.post("/public-filterable-widgets", json={"id": 2, "name": "b"})

            bad = await client.get("/public-filterable-widgets?sort=secret")
            assert bad.status_code == 422  # not an allowed enum member

            ok = await client.get("/public-filterable-widgets?sort=name")
            assert ok.status_code == 200

    @pytest.mark.filterwarnings("ignore::Warning")
    @pytest.mark.asyncio
    async def test_hidden_field_not_filterable_over_http(self, session_factory) -> None:
        """``?<hidden>__op=value`` is rejected, not silently applied (issue #62)."""
        resource = ExposedFilterableWidget()
        resource.on_register()
        resource._session_factory = session_factory
        app = FastAPI()
        register_routes(app, resource)
        register_error_handlers(app)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await client.post("/public-filterable-widgets", json={"id": 1, "name": "a"})
            await client.post("/public-filterable-widgets", json={"id": 2, "name": "b"})

            bad = await client.get("/public-filterable-widgets?secret__eq=x")
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "invalid_input"

            ok = await client.get("/public-filterable-widgets?name__eq=a")
            assert ok.status_code == 200

    @pytest.mark.filterwarnings("ignore::Warning")
    @pytest.mark.asyncio
    async def test_hidden_field_absent_from_filter_openapi(self, session_factory) -> None:
        """The hidden field is not advertised as a filter param in OpenAPI either."""
        resource = ExposedFilterableWidget()
        resource.on_register()
        resource._session_factory = session_factory
        app = FastAPI()
        register_routes(app, resource)
        register_error_handlers(app)

        schema = app.openapi()
        params = {
            p["name"] for p in schema["paths"]["/public-filterable-widgets"]["get"]["parameters"]
        }
        assert "name__eq" in params
        assert "secret__eq" not in params

    @pytest.mark.asyncio
    async def test_routes_tagged_with_exposed_name(self) -> None:
        """Routes are tagged with the exposed resource's name, not the hidden one."""
        resource = ExposedWidget()
        resource.on_register()
        router = register_routes(FastAPI(), resource)
        tags = {tag for route in router.routes for tag in getattr(route, "tags", [])}
        assert tags == {"PublicWidget"}


# ---------------------------------------------------------------------------
# DependencyBuilder seam (issue #62, decision 4)
# ---------------------------------------------------------------------------


class TestDependencyBuilderSeam:
    def test_default_builder_returns_resource_dependency(self):
        from resourcey.config.config_dependency import DefaultDependencyBuilder

        resource = WrapperWidget()
        builder = DefaultDependencyBuilder()
        assert builder.get_service_dependency(resource) == resource.get_service_dependency

    def test_framework_config_defaults_to_default_builder(self):
        from resourcey.config.config_dependency import DefaultDependencyBuilder
        from resourcey.config.config_framework import FrameworkConfig

        assert isinstance(FrameworkConfig().dependency_builder, DefaultDependencyBuilder)

    def test_configured_builder_swaps_dependency_for_every_resource(self, monkeypatch):
        """Setting FrameworkConfig.dependency_builder swaps the route dependency.

        Uses the LazyField env-var path: ``DEPENDENCY_BUILDER_CLASS`` points at a
        builder that composes a custom dependency.
        """
        from resourcey.config.config_dependency import DefaultDependencyBuilder
        from resourcey.config.config_framework import FrameworkConfig

        monkeypatch.setenv(
            "DEPENDENCY_BUILDER_CLASS",
            f"{_RecordingDependencyBuilder.__module__}._RecordingDependencyBuilder",
        )
        builder = FrameworkConfig().dependency_builder
        assert isinstance(builder, _RecordingDependencyBuilder)
        assert builder.get_service_dependency(WrapperWidget()).__name__ == "recording_dependency"

        # The default builder is unchanged and is what an unset var resolves to.
        assert DefaultDependencyBuilder().get_service_dependency(WrapperWidget()) is not None

    def test_builder_cannot_suppress_routes(self):
        """Only get_exposed_resource() gates routes; the builder never does.

        A builder that would raise if consulted must not be reached for an
        internal-only resource: exposure is decided (and the builder skipped)
        before any dependency is built.
        """
        from resourcey.config.config_framework import FrameworkConfig
        from resourcey.config.config_runtime import clear_config_cache, set_config

        cfg = FrameworkConfig()
        cfg.__dict__["_lazy_dependency_builder"] = _ExplodingDependencyBuilder()
        set_config(cfg)
        try:
            app = FastAPI()
            router = register_routes(app, InternalResource())
            assert len(router.routes) == 0

            # Sanity: the builder *is* consulted for an exposed resource.
            with pytest.raises(RuntimeError, match="builder consulted"):
                register_routes(FastAPI(), WrapperWidget())
        finally:
            clear_config_cache()

    @pytest.mark.filterwarnings("ignore::Warning")
    @pytest.mark.asyncio
    async def test_builder_composed_auth_stays_in_openapi(self, session_factory) -> None:
        """A builder-composed auth dependency still reports its security scheme.

        The scheme survives because auth stays a FastAPI ``Depends`` inside the
        callable (not a bare ASGI layer), so clients can still discover it.
        """
        from fastapi import Security
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

        from resourcey.config.config_framework import FrameworkConfig
        from resourcey.config.config_runtime import clear_config_cache, set_config

        scheme = HTTPBearer()

        class _AuthBuilder(DependencyBuilder):
            def get_service_dependency(self, resource):
                async def dependency(
                    request,
                    _creds: HTTPAuthorizationCredentials = Security(scheme),  # noqa: B008
                ):
                    async with resource.open_storage(request) as storage:
                        yield resource.build_service(resource, storage)

                return dependency

        resource = WrapperWidget()
        resource.on_register()
        resource._session_factory = session_factory

        cfg = FrameworkConfig()
        cfg.__dict__["_lazy_dependency_builder"] = _AuthBuilder()
        set_config(cfg)
        try:
            app = FastAPI()
            register_routes(app, resource)
            schema = app.openapi()
            security = schema["paths"]["/wrapper-widgets"]["get"]["security"]
            assert security == [{"HTTPBearer": []}]
        finally:
            clear_config_cache()


class _RecordingDependencyBuilder(DependencyBuilder):
    """A stand-in builder used by the LazyField-swap test."""

    def get_service_dependency(self, resource):
        return recording_dependency


class _ExplodingDependencyBuilder(DependencyBuilder):
    """A builder that raises if consulted, to pin the exposure-check ordering."""

    def get_service_dependency(self, resource):
        raise RuntimeError("builder consulted")


async def recording_dependency(request):
    yield None
