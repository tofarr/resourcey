"""Tests for the configuration system (issues #82 / #74).

Covers ``BaseConfig`` (the process-wide prefix and its latch, per-class cached
``get_instance`` typed to the owning class, the cross-class field-name/type
guard, ``clear_instance_cache``, ``generate_env_template``,
``ResourceyConfigError`` mapping) and ``LazyField`` (genuine laziness, caching,
missing/invalid/non-subclass ``_CLASS``, the list variant).

The framework does no ``.env`` loading of its own — ``config_loader`` is gone — so the
old ``load_dotenv`` section is deliberately absent; how config reaches the
environment (``uvicorn --env-file``, a wrapper script) is the app's business.
``FrameworkConfig`` / ``DbConfig`` are deferred: these tests exercise the
generic machinery only.

Uses ``monkeypatch.setenv`` / ``delenv`` — no mocks of the parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from config_helpers import (
    AppLikeConfig,
    FrameworkLikeConfig,
    IntConfig,
    LazyConfig,
    LazyDefaultConfig,
    LazyListConfig,
    RequiredConfig,
)
from config_lazy_helpers import Animal, Cat, Dog, NotAnAnimal

from resourcey.config import config_base
from resourcey.config.config_base import (
    BaseConfig,
    _reset_config_prefix,
    get_config_prefix,
    set_config_prefix,
)
from resourcey.config.lazy_field import LazyField, _unwrap_classvar
from resourcey.core.errors import ResourceyConfigError, ResourceyError


@pytest.fixture(autouse=True)
def _reset_prefix():
    """Restore the default prefix and clear the latch/caches around each case."""
    _reset_config_prefix()
    yield
    _reset_config_prefix()


# ---------------------------------------------------------------------------
# get_config_prefix / set_config_prefix
# ---------------------------------------------------------------------------


class TestConfigPrefix:
    def test_default_is_app(self):
        assert get_config_prefix() == "APP"
        assert BaseConfig.get_prefix() == "APP"

    def test_every_class_shares_the_prefix(self):
        assert FrameworkLikeConfig.get_prefix() == AppLikeConfig.get_prefix() == "APP"

    def test_set_before_first_read(self):
        set_config_prefix("BEFORE")
        assert get_config_prefix() == "BEFORE"
        assert BaseConfig.get_prefix() == "BEFORE"

    def test_read_latches(self):
        assert get_config_prefix() == "APP"
        with pytest.raises(ResourceyConfigError, match="already been read"):
            set_config_prefix("LATE")

    def test_set_clears_instance_caches(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        # A cached instance would otherwise survive a prefix change and keep
        # serving values read under the old prefix, so setting the prefix must
        # drop every class's cache. Clear the latch to reach that branch.
        _ = FrameworkLikeConfig.get_instance()
        assert "_cached_instance" in FrameworkLikeConfig.__dict__

        monkeypatch.setattr(config_base, "_config_prefix_retrieved", False)
        set_config_prefix("LATE")
        assert "_cached_instance" not in FrameworkLikeConfig.__dict__

    def test_set_before_read_changes_the_env_names(self, monkeypatch):
        # The one prefix governs every class, so a set is immediately visible
        # in the var each class parses — there is no per-class override.
        set_config_prefix("MYAPP")
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.setenv("MYAPP_BASE_URL", "https://myapp.example.com")
        assert FrameworkLikeConfig.get_instance().base_url == "https://myapp.example.com"

    def test_reset_restores_default(self):
        set_config_prefix("TEMP")
        _reset_config_prefix()
        assert get_config_prefix() == "APP"


# ---------------------------------------------------------------------------
# BaseConfig.get_instance + per-class caching
# ---------------------------------------------------------------------------


class TestGetInstance:
    def test_caches_per_class(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        first = FrameworkLikeConfig.get_instance()
        second = FrameworkLikeConfig.get_instance()
        assert first is second

    def test_return_is_typed_to_the_calling_class(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.delenv("APP_FEATURE", raising=False)
        base = FrameworkLikeConfig.get_instance()
        app = AppLikeConfig.get_instance()
        assert isinstance(base, FrameworkLikeConfig)
        assert isinstance(app, AppLikeConfig)
        # App fields are visible only through the app config.
        assert app.feature == "off"
        assert not hasattr(base, "feature")

    def test_subclass_and_base_cache_independently(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.setenv("APP_FEATURE", "on")
        app = AppLikeConfig.get_instance()
        base = FrameworkLikeConfig.get_instance()
        assert app is not base
        assert app.base_url == base.base_url
        assert app.feature == "on"

    def test_rebuilds_after_clear(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://example.com")
        first = FrameworkLikeConfig.get_instance()
        assert first.base_url == "https://example.com"

        monkeypatch.setenv("APP_BASE_URL", "https://other.com")
        # Without clearing, the stale cached instance is returned.
        assert FrameworkLikeConfig.get_instance().base_url == "https://example.com"

        FrameworkLikeConfig.clear_instance_cache()
        second = FrameworkLikeConfig.get_instance()
        assert second.base_url == "https://other.com"
        assert first is not second

    def test_reads_environ_only(self, monkeypatch, tmp_path):
        """No ``.env`` loading: a file in cwd is ignored."""
        (tmp_path / ".env").write_text("APP_BASE_URL=https://from-dotenv.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        instance = FrameworkLikeConfig.get_instance()
        assert instance.base_url == "http://localhost:8000"

    def test_missing_required_var_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("APP_NAME", raising=False)
        with pytest.raises(ResourceyConfigError, match="APP"):
            RequiredConfig.get_instance()

    def test_invalid_value_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("APP_PORT", "not-an-int")
        with pytest.raises(ResourceyConfigError, match="APP"):
            IntConfig.get_instance()

    def test_config_error_is_resourcey_error(self, monkeypatch):
        monkeypatch.delenv("APP_NAME", raising=False)
        with pytest.raises(ResourceyError):
            RequiredConfig.get_instance()


# ---------------------------------------------------------------------------
# clear_instance_cache
# ---------------------------------------------------------------------------


class TestClearInstanceCache:
    def test_clear_drops_only_that_class(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        _ = FrameworkLikeConfig.get_instance()
        _ = AppLikeConfig.get_instance()
        assert "_cached_instance" in FrameworkLikeConfig.__dict__
        assert "_cached_instance" in AppLikeConfig.__dict__

        FrameworkLikeConfig.clear_instance_cache()
        assert "_cached_instance" not in FrameworkLikeConfig.__dict__
        # A subclass is untouched by clearing its base.
        assert "_cached_instance" in AppLikeConfig.__dict__

    def test_clearing_base_does_not_clear_subclass(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        app = AppLikeConfig.get_instance()
        BaseConfig.clear_instance_cache()
        assert AppLikeConfig.get_instance() is app


# ---------------------------------------------------------------------------
# Cross-class field-name collision guard
# ---------------------------------------------------------------------------


class TestFieldCollisionGuard:
    def test_conflicting_types_raise_at_class_creation(self):
        class First(BaseConfig):
            config_collide_a: str = "a"

        with pytest.raises(TypeError, match="conflicting types"):

            class Second(BaseConfig):
                config_collide_a: int = 1

        # ``First`` is a real, usable config despite the failed sibling.
        assert First().config_collide_a == "a"

    def test_same_name_same_type_is_allowed(self):
        class First(BaseConfig):
            config_same_type: str = "a"

        class Second(BaseConfig):
            config_same_type: str = "b"

        assert Second().config_same_type == "b"
        assert First is not Second

    def test_classvar_is_not_a_field(self):
        # A ClassVar named like an existing field is not registered, so it does
        # not collide even when its inner type differs.
        class Holder(BaseConfig):
            config_classvar_pet: str = "scalar"
            config_classvar_lazy: ClassVar[Animal] = LazyField(default=Cat)

        assert Holder().config_classvar_pet == "scalar"


# ---------------------------------------------------------------------------
# generate_env_template
# ---------------------------------------------------------------------------


class TestGenerateEnvTemplate:
    def test_defaults_instance(self):
        template = FrameworkLikeConfig().generate_env_template()
        assert "APP_BASE_URL=http://localhost:8000" in template
        # Descriptions appear as comments.
        assert "Public base URL" in template

    def test_resolved_instance(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://changed.com")
        instance = FrameworkLikeConfig.get_instance()
        template = instance.generate_env_template()
        assert "APP_BASE_URL=https://changed.com" in template

    def test_explicit_prefix(self):
        template = FrameworkLikeConfig().generate_env_template(prefix="OTHER")
        assert "OTHER_BASE_URL=http://localhost:8000" in template


# ---------------------------------------------------------------------------
# LazyField
# ---------------------------------------------------------------------------


class TestLazyField:
    def test_not_resolved_at_construction(self, monkeypatch):
        # No env vars set — constructing the config must not import the subclass.
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = LazyConfig()
        # Genuine laziness: instance.__dict__ is empty after construction.
        assert "_lazy_pet" not in instance.__dict__

    def test_resolves_on_first_access(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_MEOW", "mrow")
        instance = LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Cat)
        assert pet.meow == "mrow"

    def test_cached_on_instance(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        instance = LazyConfig()
        first = instance.pet
        second = instance.pet
        assert first is second
        assert "_lazy_pet" in instance.__dict__

    def test_missing_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = LazyConfig()
        with pytest.raises(ResourceyConfigError, match="PET_CLASS"):
            _ = instance.pet

    def test_empty_class_var_raises_instead_of_falling_back(self, monkeypatch):
        """A set-but-empty var is a misconfiguration, not "use the default".

        Regression: an empty ``{NAME}_CLASS`` used to be treated as unset and
        silently resolve to the default.
        """
        monkeypatch.setenv("PET_CLASS", "")
        instance = LazyConfig()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_empty_class_var_does_not_use_default(self, monkeypatch):
        """Even with a default configured, an empty var raises rather than defaulting."""
        monkeypatch.setenv("PET_CLASS", "   ")
        instance = LazyDefaultConfig()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_unset_class_var_uses_default(self, monkeypatch):
        """An unset var (no key at all) still falls back to the default."""
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = LazyDefaultConfig()
        assert isinstance(instance.pet, Cat)

    def test_callable_default_is_not_shared(self, monkeypatch):
        monkeypatch.delenv("PET_CLASS", raising=False)
        first = LazyDefaultConfig().pet
        second = LazyDefaultConfig().pet
        assert first is not second

    def test_non_subclass_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{NotAnAnimal.__module__}.NotAnAnimal")
        instance = LazyConfig()
        with pytest.raises(ResourceyConfigError, match="Animal"):
            _ = instance.pet

    def test_dog_subclass(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Dog.__module__}.Dog")
        monkeypatch.setenv("PET_BARK", "loud")
        instance = LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Dog)
        assert pet.bark == "loud"

    def test_invalid_field_value_raises_config_error(self, monkeypatch):
        # Cat.volume is int; set a non-int value so from_env fails.
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_VOLUME", "not-an-int")
        instance = LazyConfig()
        with pytest.raises(ResourceyConfigError):
            _ = instance.pet

    def test_access_from_class_returns_descriptor(self):
        # Accessing the lazy field on the class (not an instance) returns the descriptor itself.
        assert isinstance(LazyConfig.pet, LazyField)

    def test_non_qualified_class_name_raises_config_error(self, monkeypatch):
        # A bare class name with no module path cannot be imported.
        monkeypatch.setenv("PET_CLASS", "BareClassName")
        instance = LazyConfig()
        with pytest.raises(ResourceyConfigError, match="fully-qualified"):
            _ = instance.pet


# ---------------------------------------------------------------------------
# LazyField list variant
# ---------------------------------------------------------------------------


class TestLazyFieldList:
    def test_list_from_sequential_env(self, monkeypatch):
        monkeypatch.delenv("APP_PETS", raising=False)
        monkeypatch.setenv("APP_PETS_0", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("APP_PETS_1", f"{Dog.__module__}.Dog")
        instance = LazyListConfig()
        assert instance.pets == [Cat, Dog]

    def test_list_from_json_array(self, monkeypatch):
        import json

        monkeypatch.delenv("APP_PETS_0", raising=False)
        monkeypatch.delenv("APP_PETS_1", raising=False)
        monkeypatch.setenv(
            "APP_PETS", json.dumps([f"{Cat.__module__}.Cat", f"{Dog.__module__}.Dog"])
        )
        instance = LazyListConfig()
        assert instance.pets == [Cat, Dog]

    def test_unset_list_is_empty(self, monkeypatch):
        monkeypatch.delenv("APP_PETS", raising=False)
        monkeypatch.delenv("APP_PETS_0", raising=False)
        instance = LazyListConfig()
        assert instance.pets == []

    def test_malformed_json_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("APP_PETS_0", raising=False)
        monkeypatch.setenv("APP_PETS", "{not json")
        instance = LazyListConfig()
        with pytest.raises(ResourceyConfigError, match="JSON array"):
            _ = instance.pets

    def test_non_subclass_path_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("APP_PETS_0", raising=False)
        monkeypatch.setenv("APP_PETS", '["datetime.datetime"]')
        instance = LazyListConfig()
        with pytest.raises(ResourceyConfigError, match="DiscriminatedUnionMixin"):
            _ = instance.pets


# ---------------------------------------------------------------------------
# _unwrap_classvar
# ---------------------------------------------------------------------------


class TestUnwrapClassvar:
    def test_strips_classvar(self):
        assert _unwrap_classvar(ClassVar[Animal]) is Animal

    def test_passes_through_non_classvar(self):
        assert _unwrap_classvar(Animal) is Animal


# ---------------------------------------------------------------------------
# No openhands import in the config/util modules
# ---------------------------------------------------------------------------


class TestNoOpenhandsImport:
    @pytest.mark.parametrize(
        "module",
        [
            "resourcey.core.errors",
            "resourcey.config.config_base",
            "resourcey.config.lazy_field",
            "resourcey.util.import_paths",
        ],
    )
    def test_no_openhands_import(self, module):
        """The vendored/ported modules must stay free of an SDK dependency.

        The ``openhands`` name may appear in a docstring crediting the upstream
        the code was vendored from, so this looks for an import statement.
        """
        import importlib

        mod = importlib.import_module(module)
        source = Path(mod.__file__).read_text()
        assert "import openhands" not in source
        assert "from openhands" not in source


class TestErrors:
    def test_config_error_is_resourcey_error(self):
        assert issubclass(ResourceyConfigError, ResourceyError)

    def test_core_service_errors_stay_separate(self):
        """``errors.py`` is exactly the framework-level classes.

        The service-level hierarchy (``ServiceError`` / ``NotFoundError``) is
        tracked separately and must stay where it is raised.
        """
        from resourcey.core import errors, service

        assert not issubclass(service.ServiceError, errors.ResourceyError)
        assert not issubclass(service.NotFoundError, errors.ResourceyError)

    def test_errors_module_defines_only_the_framework_classes(self):
        from resourcey.core import errors

        defined = {
            name
            for name, value in vars(errors).items()
            if isinstance(value, type) and value.__module__ == errors.__name__
        }
        assert defined == {
            "ResourceyError",
            "ResourceyConfigError",
            "InvalidInputError",
            "UnsupportedFilterError",
            "ConflictError",
        }
