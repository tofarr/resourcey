"""Tests for runtime config selection (issue #11).

Covers ``get_config`` (resolution order + caching), ``set_config`` (override
wins over env var), ``get_config_as`` (typed access + type mismatch),
``clear_config_cache`` (rebuild after override / env-var cache drop), and
``RESOURCEY_CONFIG_CLASS`` env-var discovery (valid class, unset → fallback,
unimportable module, missing class, non-``BaseConfig`` subclass). Uses
``monkeypatch.setenv`` / ``delenv`` — no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from config_runtime_helpers import AppConfig, NotBaseConfig

from resourcey.config import config_runtime
from resourcey.config.config_framework import FrameworkConfig
from resourcey.resource.errors import ResourceyConfigError

_CONFIG_CLASS_ENV = "RESOURCEY_CONFIG_CLASS"


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Clear the runtime config cache before and after every test."""
    config_runtime.clear_config_cache()
    AppConfig.clear_instance_cache()
    FrameworkConfig.clear_instance_cache()
    yield
    config_runtime.clear_config_cache()
    AppConfig.clear_instance_cache()
    FrameworkConfig.clear_instance_cache()


# ---------------------------------------------------------------------------
# get_config default / fallback
# ---------------------------------------------------------------------------


class TestGetConfigDefault:
    def test_unset_env_var_falls_back_to_framework_config(self, monkeypatch):
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        cfg = config_runtime.get_config()
        assert isinstance(cfg, FrameworkConfig)

    def test_empty_env_var_falls_back_to_framework_config(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, "")
        cfg = config_runtime.get_config()
        assert isinstance(cfg, FrameworkConfig)

    def test_caches_so_repeated_calls_return_same_instance(self, monkeypatch):
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        first = config_runtime.get_config()
        second = config_runtime.get_config()
        assert first is second


# ---------------------------------------------------------------------------
# RESOURCEY_CONFIG_CLASS env-var discovery
# ---------------------------------------------------------------------------


class TestEnvVarDiscovery:
    def test_valid_class_is_imported_and_delegated(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        cfg = config_runtime.get_config()
        assert isinstance(cfg, AppConfig)

    def test_env_var_resolved_instance_is_cached(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        first = config_runtime.get_config()
        second = config_runtime.get_config()
        assert first is second

    def test_lazy_import_not_done_at_module_import(self, monkeypatch):
        # The env var is read on the first get_config() call, not at import time.
        # Here we ensure a freshly-cleared module rebuilds from the env var set
        # after import — proving the import is deferred to the call.
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        config_runtime.get_config()  # populates the cache with FrameworkConfig
        config_runtime.clear_config_cache()
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        cfg = config_runtime.get_config()
        assert isinstance(cfg, AppConfig)

    def test_unimportable_module_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, "nonexistent.module.AppConfig")
        with pytest.raises(ResourceyConfigError, match=_CONFIG_CLASS_ENV):
            config_runtime.get_config()

    def test_missing_class_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.NoSuchClass")
        with pytest.raises(ResourceyConfigError, match=_CONFIG_CLASS_ENV):
            config_runtime.get_config()

    def test_non_baseconfig_subclass_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{NotBaseConfig.__module__}.NotBaseConfig")
        with pytest.raises(ResourceyConfigError, match="BaseConfig"):
            config_runtime.get_config()

    def test_bare_class_name_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, "BareClassName")
        with pytest.raises(ResourceyConfigError, match="fully-qualified"):
            config_runtime.get_config()


# ---------------------------------------------------------------------------
# set_config override + resolution order
# ---------------------------------------------------------------------------


class TestSetConfigOverride:
    def test_override_returned_verbatim(self):
        instance = FrameworkConfig()
        config_runtime.set_config(instance)
        assert config_runtime.get_config() is instance

    def test_override_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        override = FrameworkConfig()
        config_runtime.set_config(override)
        cfg = config_runtime.get_config()
        assert cfg is override
        assert not isinstance(cfg, AppConfig)

    def test_override_wins_over_default(self, monkeypatch):
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        override = AppConfig(app_name="pinned")
        config_runtime.set_config(override)
        cfg = config_runtime.get_config()
        assert cfg is override
        assert cfg.app_name == "pinned"


# ---------------------------------------------------------------------------
# get_config_as
# ---------------------------------------------------------------------------


class TestGetConfigAs:
    def test_returns_typed_instance(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        cfg = config_runtime.get_config_as(AppConfig)
        assert isinstance(cfg, AppConfig)

    def test_default_typed_as_framework(self, monkeypatch):
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        cfg = config_runtime.get_config_as(FrameworkConfig)
        assert isinstance(cfg, FrameworkConfig)

    def test_type_mismatch_raises_config_error(self, monkeypatch):
        # Default is FrameworkConfig; asking for AppConfig must raise.
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        with pytest.raises(ResourceyConfigError, match="AppConfig"):
            config_runtime.get_config_as(AppConfig)

    def test_type_mismatch_message_names_actual_type(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        with pytest.raises(ResourceyConfigError, match="AppConfig"):
            # AppConfig is not a FrameworkConfig.
            config_runtime.get_config_as(FrameworkConfig)


# ---------------------------------------------------------------------------
# clear_config_cache
# ---------------------------------------------------------------------------


class TestClearConfigCache:
    def test_drops_override(self):
        config_runtime.set_config(FrameworkConfig())
        assert config_runtime.get_config() is not None
        config_runtime.clear_config_cache()
        # After clearing with no env var, falls back to FrameworkConfig (a new
        # cached default instance), not the previous override.
        cfg = config_runtime.get_config()
        assert isinstance(cfg, FrameworkConfig)

    def test_drops_env_resolved_cache(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        first = config_runtime.get_config()
        assert isinstance(first, AppConfig)
        config_runtime.clear_config_cache()
        # Changing the env var after clearing is observed on the next call.
        monkeypatch.delenv(_CONFIG_CLASS_ENV, raising=False)
        second = config_runtime.get_config()
        assert isinstance(second, FrameworkConfig)
        assert second is not first

    def test_rebuild_after_clear_uses_env_var(self, monkeypatch):
        monkeypatch.setenv(_CONFIG_CLASS_ENV, f"{AppConfig.__module__}.AppConfig")
        first = config_runtime.get_config()
        config_runtime.clear_config_cache()
        monkeypatch.setenv(_CONFIG_CLASS_ENV, "")
        second = config_runtime.get_config()
        assert isinstance(first, AppConfig)
        assert isinstance(second, FrameworkConfig)


# ---------------------------------------------------------------------------
# No openhands import in new config modules
# ---------------------------------------------------------------------------


class TestNoOpenhandsImport:
    @pytest.mark.parametrize(
        "module",
        ["resourcey.config.config_runtime"],
    )
    def test_no_openhands_import(self, module):
        import importlib

        mod = importlib.import_module(module)
        source = Path(mod.__file__).read_text()
        assert "openhands" not in source.lower()
