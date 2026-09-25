"""Tests for the ``v2`` configuration system (issue #82).

Covers ``BaseConfig`` (prefix, per-class cached ``get_instance`` typed to the
owning class, per-class ``clear_instance_cache``, ``generate_env_template``,
``ResourceyConfigError`` mapping), ``load_dotenv`` (present/absent/quoted/
commented, env-over-file precedence), and ``LazyField`` (genuine laziness,
caching, missing/invalid/non-subclass ``_CLASS``, the list variant). Uses
``monkeypatch.setenv`` / ``delenv`` — no mocks of the parser.

The ``FrameworkConfig`` / ``DbConfig`` sections of v1's ``test_config_system``
are deliberately absent: ``FrameworkConfig`` is deferred to a later PR, and
these tests exercise the generic machinery only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest
from v2_config_helpers import (
    V2AppLikeConfig,
    V2FrameworkLikeConfig,
    V2IntConfig,
    V2LazyConfig,
    V2LazyDefaultConfig,
    V2LazyListConfig,
    V2RequiredConfig,
)
from v2_config_lazy_helpers import Animal, Cat, Dog, NotAnAnimal

from resourcey.v2.config.config_base import BaseConfig
from resourcey.v2.config.config_loader import _strip_inline_comment, _unquote, load_dotenv
from resourcey.v2.config.lazy_field import LazyField, _unwrap_classvar
from resourcey.v2.core.errors import ResourceyConfigError, ResourceyError

# ---------------------------------------------------------------------------
# BaseConfig.get_prefix
# ---------------------------------------------------------------------------


class TestGetPrefix:
    def test_default_top_level_module_uppercased(self):
        # BaseConfig lives in resourcey.v2.config.config_base -> "RESOURCEY".
        assert BaseConfig.get_prefix() == "RESOURCEY"

    def test_default_uses_module_name(self):
        class Cfg(BaseConfig):
            pass

        assert Cfg.get_prefix() == Cfg.__module__.split(".")[0].upper()

    def test_override(self):
        class Cfg(BaseConfig):
            @classmethod
            def get_prefix(cls) -> str:
                return "CUSTOM"

        assert Cfg.get_prefix() == "CUSTOM"


# ---------------------------------------------------------------------------
# BaseConfig.get_instance + per-class caching
# ---------------------------------------------------------------------------


class TestGetInstance:
    def test_caches_per_class(self, monkeypatch):
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        V2FrameworkLikeConfig.clear_instance_cache()
        first = V2FrameworkLikeConfig.get_instance()
        second = V2FrameworkLikeConfig.get_instance()
        assert first is second

    def test_return_is_typed_to_the_calling_class(self, monkeypatch):
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        monkeypatch.delenv("V2CFG_FEATURE", raising=False)
        V2FrameworkLikeConfig.clear_instance_cache()
        V2AppLikeConfig.clear_instance_cache()
        base = V2FrameworkLikeConfig.get_instance()
        app = V2AppLikeConfig.get_instance()
        assert isinstance(base, V2FrameworkLikeConfig)
        assert isinstance(app, V2AppLikeConfig)
        # App fields are visible only through the app config.
        assert app.feature == "off"
        assert not hasattr(base, "feature")

    def test_subclass_and_base_cache_independently(self, monkeypatch):
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        monkeypatch.setenv("V2CFG_FEATURE", "on")
        V2FrameworkLikeConfig.clear_instance_cache()
        V2AppLikeConfig.clear_instance_cache()
        app = V2AppLikeConfig.get_instance()
        base = V2FrameworkLikeConfig.get_instance()
        assert app is not base
        assert app.base_url == base.base_url
        assert app.feature == "on"

    def test_rebuilds_after_clear(self, monkeypatch):
        monkeypatch.setenv("V2CFG_BASE_URL", "https://example.com")
        V2FrameworkLikeConfig.clear_instance_cache()
        first = V2FrameworkLikeConfig.get_instance()
        assert first.base_url == "https://example.com"

        monkeypatch.setenv("V2CFG_BASE_URL", "https://other.com")
        # Without clearing, the stale cached instance is returned.
        assert V2FrameworkLikeConfig.get_instance().base_url == "https://example.com"

        V2FrameworkLikeConfig.clear_instance_cache()
        second = V2FrameworkLikeConfig.get_instance()
        assert second.base_url == "https://other.com"
        assert first is not second

    def test_folds_dotenv_into_environ(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("V2CFG_BASE_URL=https://from-dotenv.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        V2FrameworkLikeConfig.clear_instance_cache()
        instance = V2FrameworkLikeConfig.get_instance()
        assert instance.base_url == "https://from-dotenv.com"

    def test_dotenv_path_override_via_env_file(self, monkeypatch, tmp_path):
        env_file = tmp_path / "custom.env"
        env_file.write_text("V2CFG_BASE_URL=https://custom.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RESOURCEY_ENV_FILE", str(env_file))
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        V2FrameworkLikeConfig.clear_instance_cache()
        instance = V2FrameworkLikeConfig.get_instance()
        assert instance.base_url == "https://custom.com"

    def test_env_overrides_dotenv(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("V2CFG_BASE_URL=https://from-dotenv.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("V2CFG_BASE_URL", "https://from-env.com")
        V2FrameworkLikeConfig.clear_instance_cache()
        instance = V2FrameworkLikeConfig.get_instance()
        assert instance.base_url == "https://from-env.com"

    def test_missing_required_var_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("V2REQ_NAME", raising=False)
        V2RequiredConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="V2REQ"):
            V2RequiredConfig.get_instance()

    def test_invalid_value_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("V2INT_PORT", "not-an-int")
        V2IntConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="V2INT"):
            V2IntConfig.get_instance()

    def test_config_error_is_resourcey_error(self, monkeypatch):
        monkeypatch.delenv("V2REQ_NAME", raising=False)
        V2RequiredConfig.clear_instance_cache()
        with pytest.raises(ResourceyError):
            V2RequiredConfig.get_instance()


# ---------------------------------------------------------------------------
# clear_instance_cache
# ---------------------------------------------------------------------------


class TestClearInstanceCache:
    def test_clear_drops_only_that_class(self, monkeypatch):
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        V2FrameworkLikeConfig.clear_instance_cache()
        V2AppLikeConfig.clear_instance_cache()
        _ = V2FrameworkLikeConfig.get_instance()
        _ = V2AppLikeConfig.get_instance()
        assert "_cached_instance" in V2FrameworkLikeConfig.__dict__
        assert "_cached_instance" in V2AppLikeConfig.__dict__

        V2FrameworkLikeConfig.clear_instance_cache()
        assert "_cached_instance" not in V2FrameworkLikeConfig.__dict__
        # A subclass is untouched by clearing its base.
        assert "_cached_instance" in V2AppLikeConfig.__dict__

    def test_clearing_base_does_not_clear_subclass(self, monkeypatch):
        monkeypatch.delenv("V2CFG_BASE_URL", raising=False)
        V2AppLikeConfig.clear_instance_cache()
        app = V2AppLikeConfig.get_instance()
        BaseConfig.clear_instance_cache()
        assert V2AppLikeConfig.get_instance() is app


# ---------------------------------------------------------------------------
# generate_env_template
# ---------------------------------------------------------------------------


class TestGenerateEnvTemplate:
    def test_defaults_instance(self):
        template = V2FrameworkLikeConfig().generate_env_template()
        assert "V2CFG_BASE_URL=http://localhost:8000" in template
        # Descriptions appear as comments.
        assert "Public base URL" in template

    def test_resolved_instance(self, monkeypatch):
        monkeypatch.setenv("V2CFG_BASE_URL", "https://changed.com")
        V2FrameworkLikeConfig.clear_instance_cache()
        instance = V2FrameworkLikeConfig.get_instance()
        template = instance.generate_env_template()
        assert "V2CFG_BASE_URL=https://changed.com" in template

    def test_explicit_prefix(self):
        template = V2FrameworkLikeConfig().generate_env_template(prefix="OTHER")
        assert "OTHER_BASE_URL=http://localhost:8000" in template


# ---------------------------------------------------------------------------
# load_dotenv
# ---------------------------------------------------------------------------


class TestLoadDotenv:
    def test_present(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("FOO=bar\nBAZ=qux\n")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)
        load_dotenv(path)
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_absent_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("RESOURCEY_ENV_FILE", raising=False)
        # No file present — should not raise.
        load_dotenv()

    def test_env_overrides_file(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("FOO=fromfile\n")
        monkeypatch.setenv("FOO", "fromenv")
        load_dotenv(path)
        assert os.environ["FOO"] == "fromenv"

    def test_comments_and_blanks_skipped(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# a comment\n\nKEY=val\n")
        monkeypatch.delenv("KEY", raising=False)
        load_dotenv(path)
        assert os.environ["KEY"] == "val"

    def test_quoted_values_unquoted(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("A=\"value\"\nB='single'\nC=`back`\nD=plain\n")
        for k in ("A", "B", "C", "D"):
            monkeypatch.delenv(k, raising=False)
        load_dotenv(path)
        assert os.environ["A"] == "value"
        assert os.environ["B"] == "single"
        assert os.environ["C"] == "back"
        assert os.environ["D"] == "plain"

    def test_uses_resourcey_env_file(self, monkeypatch, tmp_path):
        path = tmp_path / "custom.env"
        path.write_text("FROMCUSTOM=1\n")
        monkeypatch.setenv("RESOURCEY_ENV_FILE", str(path))
        monkeypatch.delenv("FROMCUSTOM", raising=False)
        load_dotenv()
        assert os.environ["FROMCUSTOM"] == "1"

    def test_unquote_helpers(self):
        assert _unquote('"x"') == "x"
        assert _unquote("'y'") == "y"
        assert _unquote("`z`") == "z"
        assert _unquote("plain") == "plain"
        assert _unquote('"unmatched') == '"unmatched'
        assert _unquote('""') == ""

    def test_inline_comment_stripped(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("KEY=val # a comment\nNUM=42 #another\n")
        monkeypatch.delenv("KEY", raising=False)
        monkeypatch.delenv("NUM", raising=False)
        load_dotenv(path)
        assert os.environ["KEY"] == "val"
        assert os.environ["NUM"] == "42"

    def test_inline_comment_requires_preceding_whitespace(self, monkeypatch, tmp_path):
        # `#` not preceded by whitespace is part of the value (e.g. a password).
        path = tmp_path / ".env"
        path.write_text("PW=p#ass\nURL=https://x/#frag\n")
        monkeypatch.delenv("PW", raising=False)
        monkeypatch.delenv("URL", raising=False)
        load_dotenv(path)
        assert os.environ["PW"] == "p#ass"
        assert os.environ["URL"] == "https://x/#frag"

    def test_inline_comment_inside_quotes_preserved(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text('MSG="hi # there"\n')
        monkeypatch.delenv("MSG", raising=False)
        load_dotenv(path)
        assert os.environ["MSG"] == "hi # there"

    def test_strip_inline_comment_helpers(self):
        assert _strip_inline_comment("val # comment") == "val"
        assert _strip_inline_comment("val#comment") == "val#comment"
        assert _strip_inline_comment("p#ass") == "p#ass"
        assert _strip_inline_comment('"a # b"') == '"a # b"'
        assert _strip_inline_comment("plain") == "plain"
        assert _strip_inline_comment("") == ""

    def test_line_without_equals_skipped(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("NOEQUALSSIGN\nKEY=val\n")
        monkeypatch.delenv("KEY", raising=False)
        monkeypatch.delenv("NOEQUALSSIGN", raising=False)
        load_dotenv(path)
        assert os.environ["KEY"] == "val"

    def test_line_with_empty_key_skipped(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text("=value\nKEY=val\n")
        monkeypatch.delenv("KEY", raising=False)
        load_dotenv(path)
        assert os.environ["KEY"] == "val"


# ---------------------------------------------------------------------------
# LazyField
# ---------------------------------------------------------------------------


class TestLazyField:
    def test_not_resolved_at_construction(self, monkeypatch):
        # No env vars set — constructing the config must not import the subclass.
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = V2LazyConfig()
        # Genuine laziness: instance.__dict__ is empty after construction.
        assert "_lazy_pet" not in instance.__dict__

    def test_resolves_on_first_access(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_MEOW", "mrow")
        instance = V2LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Cat)
        assert pet.meow == "mrow"

    def test_cached_on_instance(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        instance = V2LazyConfig()
        first = instance.pet
        second = instance.pet
        assert first is second
        assert "_lazy_pet" in instance.__dict__

    def test_missing_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = V2LazyConfig()
        with pytest.raises(ResourceyConfigError, match="PET_CLASS"):
            _ = instance.pet

    def test_empty_class_var_raises_instead_of_falling_back(self, monkeypatch):
        """A set-but-empty var is a misconfiguration, not "use the default".

        Regression: an empty ``{NAME}_CLASS`` used to be treated as unset and
        silently resolve to the default.
        """
        monkeypatch.setenv("PET_CLASS", "")
        instance = V2LazyConfig()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_empty_class_var_does_not_use_default(self, monkeypatch):
        """Even with a default configured, an empty var raises rather than defaulting."""
        monkeypatch.setenv("PET_CLASS", "   ")
        instance = V2LazyDefaultConfig()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_unset_class_var_uses_default(self, monkeypatch):
        """An unset var (no key at all) still falls back to the default."""
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = V2LazyDefaultConfig()
        assert isinstance(instance.pet, Cat)

    def test_callable_default_is_not_shared(self, monkeypatch):
        monkeypatch.delenv("PET_CLASS", raising=False)
        first = V2LazyDefaultConfig().pet
        second = V2LazyDefaultConfig().pet
        assert first is not second

    def test_non_subclass_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{NotAnAnimal.__module__}.NotAnAnimal")
        instance = V2LazyConfig()
        with pytest.raises(ResourceyConfigError, match="Animal"):
            _ = instance.pet

    def test_dog_subclass(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Dog.__module__}.Dog")
        monkeypatch.setenv("PET_BARK", "loud")
        instance = V2LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Dog)
        assert pet.bark == "loud"

    def test_invalid_field_value_raises_config_error(self, monkeypatch):
        # Cat.volume is int; set a non-int value so from_env fails.
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_VOLUME", "not-an-int")
        instance = V2LazyConfig()
        with pytest.raises(ResourceyConfigError):
            _ = instance.pet

    def test_access_from_class_returns_descriptor(self):
        # Accessing the lazy field on the class (not an instance) returns the descriptor itself.
        assert isinstance(V2LazyConfig.pet, LazyField)

    def test_non_qualified_class_name_raises_config_error(self, monkeypatch):
        # A bare class name with no module path cannot be imported.
        monkeypatch.setenv("PET_CLASS", "BareClassName")
        instance = V2LazyConfig()
        with pytest.raises(ResourceyConfigError, match="fully-qualified"):
            _ = instance.pet


# ---------------------------------------------------------------------------
# LazyField list variant
# ---------------------------------------------------------------------------


class TestLazyFieldList:
    def test_list_from_sequential_env(self, monkeypatch):
        monkeypatch.delenv("V2LAZYLIST_PETS", raising=False)
        monkeypatch.setenv("V2LAZYLIST_PETS_0", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("V2LAZYLIST_PETS_1", f"{Dog.__module__}.Dog")
        instance = V2LazyListConfig()
        assert instance.pets == [Cat, Dog]

    def test_list_from_json_array(self, monkeypatch):
        import json

        monkeypatch.delenv("V2LAZYLIST_PETS_0", raising=False)
        monkeypatch.delenv("V2LAZYLIST_PETS_1", raising=False)
        monkeypatch.setenv(
            "V2LAZYLIST_PETS", json.dumps([f"{Cat.__module__}.Cat", f"{Dog.__module__}.Dog"])
        )
        instance = V2LazyListConfig()
        assert instance.pets == [Cat, Dog]

    def test_unset_list_is_empty(self, monkeypatch):
        monkeypatch.delenv("V2LAZYLIST_PETS", raising=False)
        monkeypatch.delenv("V2LAZYLIST_PETS_0", raising=False)
        instance = V2LazyListConfig()
        assert instance.pets == []

    def test_malformed_json_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("V2LAZYLIST_PETS_0", raising=False)
        monkeypatch.setenv("V2LAZYLIST_PETS", "{not json")
        instance = V2LazyListConfig()
        with pytest.raises(ResourceyConfigError, match="JSON array"):
            _ = instance.pets

    def test_non_subclass_path_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("V2LAZYLIST_PETS_0", raising=False)
        monkeypatch.setenv("V2LAZYLIST_PETS", '["datetime.datetime"]')
        instance = V2LazyListConfig()
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
# No openhands import in new v2 config/util modules
# ---------------------------------------------------------------------------


class TestNoOpenhandsImport:
    @pytest.mark.parametrize(
        "module",
        [
            "resourcey.v2.core.errors",
            "resourcey.v2.config.config_base",
            "resourcey.v2.config.config_loader",
            "resourcey.v2.config.lazy_field",
            "resourcey.v2.util.import_paths",
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
        from resourcey.v2.core import errors, service

        assert not issubclass(service.ServiceError, errors.ResourceyError)
        assert not issubclass(service.NotFoundError, errors.ResourceyError)

    def test_errors_module_defines_only_the_framework_classes(self):
        from resourcey.v2.core import errors

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
        }
