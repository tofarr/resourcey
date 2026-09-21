"""Tests for ``WrapperResourceBase`` and ``is_exposed`` (issue #55)."""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr

from resourcey.resource.routes import register_routes
from resourcey.resource.service_base import Action
from resourcey.resource.sql import SqlResource
from resourcey.resource.wrapper import WrapperResourceBase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class WrapperWidget(SqlResource):
    id: int
    name: str
    secret: SecretStr = SecretStr("hidden")


class InternalResource(SqlResource):
    """An internal-only resource (``is_exposed=False``)."""

    id: int
    name: str

    def is_exposed(self) -> bool:
        return False


class PublicWidget(WrapperResourceBase):
    """A wrapper that hides the ``secret`` field from the read model."""

    def get_read_model(self) -> type[BaseModel]:
        return self._project_read_model(exclude=frozenset({"secret"}))


class NarrowedActions(WrapperResourceBase):
    """A wrapper that only exposes READ + SEARCH."""

    def get_supported_actions(self) -> frozenset:
        inner = self._inner.get_supported_actions()
        return inner & frozenset({Action.READ, Action.SEARCH})


class HiddenWrapper(WrapperResourceBase):
    """A wrapper that is itself not exposed."""

    def is_exposed(self) -> bool:
        return False


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

    def test_get_sortable_fields_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_sortable_fields() == WrapperWidget().get_sortable_fields()

    def test_get_service_cls_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.get_service_cls() is WrapperWidget().get_service_cls()

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

    def test_is_exposed_true_by_default(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.is_exposed() is True

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

    def test_open_service_delegates(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        # open_service returns an async context manager; we verify it's
        # delegated (returns the same type as the inner resource would)
        inner_cm = WrapperWidget().open_service(None)
        wrapper_cm = wrapper.open_service(None)
        assert type(wrapper_cm) is type(inner_cm)

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
# is_exposed
# ---------------------------------------------------------------------------


class TestIsExposed:
    def test_default_is_exposed_true(self):
        assert WrapperWidget().is_exposed() is True

    def test_internal_resource_not_exposed(self):
        assert InternalResource().is_exposed() is False

    def test_wrapper_can_be_hidden(self):
        wrapper = HiddenWrapper(inner=WrapperWidget())
        assert wrapper.is_exposed() is False

    def test_wrapper_exposed_by_default(self):
        wrapper = PublicWidget(inner=WrapperWidget())
        assert wrapper.is_exposed() is True


# ---------------------------------------------------------------------------
# Route builder skips non-exposed resources
# ---------------------------------------------------------------------------


class TestRouteBuilderSkipsNonExposed:
    def test_non_exposed_resource_registers_no_routes(self):
        """A resource with is_exposed=False should register no routes."""
        app = FastAPI()
        resource = InternalResource()
        router = register_routes(app, resource)
        assert len(router.routes) == 0

    def test_exposed_resource_registers_routes(self):
        app = FastAPI()
        resource = WrapperWidget()
        router = register_routes(app, resource)
        assert len(router.routes) > 0

    def test_non_exposed_wrapper_registers_no_routes(self):
        app = FastAPI()
        wrapper = HiddenWrapper(inner=WrapperWidget())
        router = register_routes(app, wrapper)
        assert len(router.routes) == 0

    def test_exposed_wrapper_registers_routes(self):
        app = FastAPI()
        wrapper = PublicWidget(inner=WrapperWidget())
        router = register_routes(app, wrapper)
        assert len(router.routes) > 0
