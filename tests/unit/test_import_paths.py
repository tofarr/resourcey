"""Tests for ``resourcey.util.import_paths`` and the LazyField list-of-types
variant (issue #21).

Covers dotted-path resolution (valid, bare-name, unimportable, missing attr,
non-subclass) and the ``ClassVar[list[type[Base]]]`` lazy field reading both
the JSON-array and sequential env forms, plus caching and base enforcement.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from app_resources import AppGadget, AppWidget
from config_lazy_helpers import Animal, Cat

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.resource.base import BaseResource
from resourcey.resource.errors import ResourceyConfigError
from resourcey.util.import_paths import resolve_import_path, resolve_import_paths

# ---------------------------------------------------------------------------
# resolve_import_path / resolve_import_paths
# ---------------------------------------------------------------------------


class TestResolveImportPath:
    def test_resolves_class(self):
        assert resolve_import_path(f"{AppWidget.__module__}.AppWidget") is AppWidget

    def test_resolves_function(self):
        import json

        assert resolve_import_path("json.loads") is json.loads

    def test_bare_name_raises_value_error(self):
        with pytest.raises(ValueError, match="fully-qualified"):
            resolve_import_path("BareName")

    def test_unimportable_module_raises_import_error(self):
        with pytest.raises(ImportError):
            resolve_import_path("nonexistent.module.Thing")

    def test_missing_attribute_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            resolve_import_path(f"{AppWidget.__module__}.NoSuchAttr")


class TestResolveImportPaths:
    def test_resolves_list_with_base_check(self):
        result = resolve_import_paths(
            [f"{AppWidget.__module__}.AppWidget", f"{AppGadget.__module__}.AppGadget"],
            base=BaseResource,
        )
        assert result == [AppWidget, AppGadget]

    def test_non_subclass_raises_type_error(self):
        with pytest.raises(TypeError, match="BaseResource"):
            resolve_import_paths(["datetime.datetime"], base=BaseResource)

    def test_empty_list_returns_empty(self):
        assert resolve_import_paths([], base=BaseResource) == []

    def test_no_base_skips_subclass_check(self):
        import json

        assert resolve_import_paths(["json.loads"]) == [json.loads]


# ---------------------------------------------------------------------------
# LazyField list-of-types variant
# ---------------------------------------------------------------------------


class _ResourcesConfig(BaseConfig):
    """Config with a lazy list-of-resources field (prefix ``RESOURCES``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "TESTCFG"

    resources: ClassVar[list[type[BaseResource]]] = LazyField()


class TestLazyFieldResources:
    def test_not_resolved_at_construction(self, monkeypatch):
        monkeypatch.delenv("TESTCFG_RESOURCES", raising=False)
        for i in range(5):
            monkeypatch.delenv(f"TESTCFG_RESOURCES_{i}", raising=False)
        instance = _ResourcesConfig()
        assert "_lazy_resources" not in instance.__dict__

    def test_resolves_json_array(self, monkeypatch):
        monkeypatch.setenv(
            "TESTCFG_RESOURCES",
            f'["{AppWidget.__module__}.AppWidget","{AppGadget.__module__}.AppGadget"]',
        )
        instance = _ResourcesConfig()
        assert instance.resources == [AppWidget, AppGadget]

    def test_resolves_sequential(self, monkeypatch):
        monkeypatch.delenv("TESTCFG_RESOURCES", raising=False)
        monkeypatch.setenv("TESTCFG_RESOURCES_0", f"{AppWidget.__module__}.AppWidget")
        monkeypatch.setenv("TESTCFG_RESOURCES_1", f"{AppGadget.__module__}.AppGadget")
        instance = _ResourcesConfig()
        assert instance.resources == [AppWidget, AppGadget]

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("TESTCFG_RESOURCES", raising=False)
        for i in range(5):
            monkeypatch.delenv(f"TESTCFG_RESOURCES_{i}", raising=False)
        instance = _ResourcesConfig()
        assert instance.resources == []

    def test_cached_on_instance(self, monkeypatch):
        monkeypatch.setenv("TESTCFG_RESOURCES", f'["{AppWidget.__module__}.AppWidget"]')
        instance = _ResourcesConfig()
        first = instance.resources
        second = instance.resources
        assert first is second
        assert "_lazy_resources" in instance.__dict__

    def test_invalid_path_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TESTCFG_RESOURCES", '["nonexistent.module.Nope"]')
        instance = _ResourcesConfig()
        with pytest.raises(ResourceyConfigError, match="resources"):
            _ = instance.resources

    def test_non_subclass_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TESTCFG_RESOURCES", '["datetime.datetime"]')
        instance = _ResourcesConfig()
        with pytest.raises(ResourceyConfigError):
            _ = instance.resources

    def test_access_from_class_returns_descriptor(self):
        assert isinstance(_ResourcesConfig.resources, LazyField)


# ---------------------------------------------------------------------------
# Single-class LazyField still works (regression guard)
# ---------------------------------------------------------------------------


class _SingleLazyConfig(BaseConfig):
    @classmethod
    def get_prefix(cls) -> str:
        return "SINGLECFG"

    pet: ClassVar[Animal] = LazyField()


class TestSingleLazyFieldRegression:
    def test_single_class_field_still_resolves(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        instance = _SingleLazyConfig()
        assert isinstance(instance.pet, Cat)
