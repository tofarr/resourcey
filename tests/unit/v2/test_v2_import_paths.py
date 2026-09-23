"""Tests for ``resourcey.v2.util.import_paths`` and the single-class LazyField
variant.

Covers dotted-path resolution (valid, bare-name, unimportable, missing attr,
non-subclass), the colon-form (``module:attr``), and the single-class
``ClassVar[Base]`` lazy field.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from v2_config_lazy_helpers import Animal, Cat
from v2_import_path_helpers import V2PathBase, V2PathGadget, V2PathWidget

from resourcey.v2.config.config_base import BaseConfig
from resourcey.v2.config.lazy_field import LazyField
from resourcey.v2.util.import_paths import resolve_import_path, resolve_import_paths

# ---------------------------------------------------------------------------
# resolve_import_path / resolve_import_paths
# ---------------------------------------------------------------------------


class TestResolveImportPath:
    def test_resolves_class(self):
        assert resolve_import_path(f"{V2PathWidget.__module__}.V2PathWidget") is V2PathWidget

    def test_resolves_class_colon_form(self):
        assert resolve_import_path(f"{V2PathWidget.__module__}:V2PathWidget") is V2PathWidget

    def test_resolves_function(self):
        import json

        assert resolve_import_path("json.loads") is json.loads

    def test_resolves_function_colon_form(self):
        import json

        assert resolve_import_path("json:loads") is json.loads

    def test_bare_name_raises_value_error(self):
        with pytest.raises(ValueError, match="fully-qualified"):
            resolve_import_path("BareName")

    def test_unimportable_module_raises_import_error(self):
        with pytest.raises(ImportError):
            resolve_import_path("nonexistent.module.Thing")

    def test_missing_attribute_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            resolve_import_path(f"{V2PathWidget.__module__}.NoSuchAttr")


class TestResolveImportPaths:
    def test_resolves_list_with_base_check(self):
        result = resolve_import_paths(
            [
                f"{V2PathGadget.__module__}.V2PathGadget",
                f"{V2PathGadget.__module__}.V2PathGadget",
            ],
            base=V2PathBase,
        )
        assert all(item is V2PathGadget for item in result)

    def test_non_subclass_raises_type_error(self):
        with pytest.raises(TypeError, match="V2PathBase"):
            resolve_import_paths(["datetime.datetime"], base=V2PathBase)

    def test_empty_list_returns_empty(self):
        assert resolve_import_paths([], base=V2PathBase) == []

    def test_no_base_skips_subclass_check(self):
        import json

        assert resolve_import_paths(["json.loads"]) == [json.loads]


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
