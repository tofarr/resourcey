"""Tests for the configuration system (issue #5).

Covers ``BaseConfig`` (prefix, cached ``get_instance``, ``clear_instance_cache``,
``generate_env_template``), ``load_dotenv`` (present/absent/quoted/commented,
env-over-file precedence), ``FrameworkConfig`` / ``DbConfig`` (defaults, nested
overrides, ``database_url`` assembly, ``cors_origins`` JSON vs sequential),
error mapping (missing required var, invalid int, out-of-range port), and
``LazyField`` (genuine laziness, caching, missing/invalid/non-subclass
``_CLASS``). Uses ``monkeypatch.setenv`` / ``delenv`` — no mocks of the parser.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest
from config_lazy_helpers import Animal, Cat, Dog, NotAnAnimal
from pydantic import SecretStr

from resourcey.config.config_base import BaseConfig
from resourcey.config.config_framework import DbConfig, FrameworkConfig
from resourcey.config.config_loader import _strip_inline_comment, _unquote, load_dotenv
from resourcey.config.lazy_field import LazyField, _unwrap_classvar
from resourcey.resource.errors import ResourceyConfigError, ResourceyError

# ---------------------------------------------------------------------------
# BaseConfig.get_prefix
# ---------------------------------------------------------------------------


class TestGetPrefix:
    def test_default_top_level_module_uppercased(self):
        # FrameworkConfig lives in resourcey.config.config_framework -> "RESOURCEY".
        assert FrameworkConfig.get_prefix() == "RESOURCEY"

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
# BaseConfig.get_instance + caching
# ---------------------------------------------------------------------------


class TestGetInstance:
    def test_caches_on_class(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_BASE_URL", raising=False)
        FrameworkConfig.clear_instance_cache()
        first = FrameworkConfig.get_instance()
        second = FrameworkConfig.get_instance()
        assert first is second

    def test_rebuilds_after_clear(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_BASE_URL", "https://example.com")
        FrameworkConfig.clear_instance_cache()
        first = FrameworkConfig.get_instance()
        assert first.base_url == "https://example.com"

        monkeypatch.setenv("RESOURCEY_BASE_URL", "https://other.com")
        # Without clearing, the stale cached instance is returned.
        assert FrameworkConfig.get_instance().base_url == "https://example.com"

        FrameworkConfig.clear_instance_cache()
        second = FrameworkConfig.get_instance()
        assert second.base_url == "https://other.com"
        assert first is not second

    def test_folds_dotenv_into_environ(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("RESOURCEY_BASE_URL=https://from-dotenv.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("RESOURCEY_ENV_FILE", raising=False)
        monkeypatch.delenv("RESOURCEY_BASE_URL", raising=False)
        FrameworkConfig.clear_instance_cache()
        instance = FrameworkConfig.get_instance()
        assert instance.base_url == "https://from-dotenv.com"

    def test_dotenv_path_override_via_env_file(self, monkeypatch, tmp_path):
        env_file = tmp_path / "custom.env"
        env_file.write_text("RESOURCEY_BASE_URL=https://custom.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RESOURCEY_ENV_FILE", str(env_file))
        monkeypatch.delenv("RESOURCEY_BASE_URL", raising=False)
        FrameworkConfig.clear_instance_cache()
        instance = FrameworkConfig.get_instance()
        assert instance.base_url == "https://custom.com"

    def test_env_overrides_dotenv(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("RESOURCEY_BASE_URL=https://from-dotenv.com\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("RESOURCEY_ENV_FILE", raising=False)
        monkeypatch.setenv("RESOURCEY_BASE_URL", "https://from-env.com")
        FrameworkConfig.clear_instance_cache()
        instance = FrameworkConfig.get_instance()
        assert instance.base_url == "https://from-env.com"

    def test_missing_required_var_raises_config_error(self, monkeypatch):
        class RequiredCfg(BaseConfig):
            @classmethod
            def get_prefix(cls) -> str:
                return "REQCFG"

            name: str

        monkeypatch.delenv("REQCFG_NAME", raising=False)
        RequiredCfg.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="REQCFG"):
            RequiredCfg.get_instance()


# ---------------------------------------------------------------------------
# clear_instance_cache
# ---------------------------------------------------------------------------


class TestClearInstanceCache:
    def test_clear_drops_cache(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_BASE_URL", raising=False)
        FrameworkConfig.clear_instance_cache()
        _ = FrameworkConfig.get_instance()
        assert "_cached_instance" in FrameworkConfig.__dict__
        FrameworkConfig.clear_instance_cache()
        assert "_cached_instance" not in FrameworkConfig.__dict__

    def test_clear_on_base_clears_subclasses(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_BASE_URL", raising=False)
        FrameworkConfig.clear_instance_cache()
        _ = FrameworkConfig.get_instance()
        assert "_cached_instance" in FrameworkConfig.__dict__
        BaseConfig.clear_instance_cache()
        assert "_cached_instance" not in FrameworkConfig.__dict__


# ---------------------------------------------------------------------------
# generate_env_template
# ---------------------------------------------------------------------------


class TestGenerateEnvTemplate:
    def test_defaults_instance(self):
        template = FrameworkConfig().generate_env_template()
        assert "RESOURCEY_BASE_URL=http://localhost:8000" in template
        assert (
            "RESOURCEY_DATABASE_URL=postgresql+asyncpg://resourcey@localhost:5432/resourcey"
            in template
        )
        # Descriptions appear as comments.
        assert "Public base URL" in template

    def test_resolved_instance(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_BASE_URL", "https://changed.com")
        FrameworkConfig.clear_instance_cache()
        instance = FrameworkConfig.get_instance()
        template = instance.generate_env_template()
        assert "RESOURCEY_BASE_URL=https://changed.com" in template

    def test_explicit_prefix(self):
        template = FrameworkConfig().generate_env_template(prefix="OTHER")
        assert "OTHER_BASE_URL=http://localhost:8000" in template


# ---------------------------------------------------------------------------
# FrameworkConfig / DbConfig
# ---------------------------------------------------------------------------


class TestFrameworkConfig:
    def test_defaults_only(self):
        cfg = FrameworkConfig()
        assert cfg.base_url == "http://localhost:8000"
        assert cfg.cors_origins == []
        assert isinstance(cfg.database, DbConfig)

    def test_nested_db_overrides(self, monkeypatch):
        url = "postgresql+asyncpg://resourcey@db.example.com:6543/resourcey"
        monkeypatch.setenv("RESOURCEY_DATABASE_URL", url)
        monkeypatch.setenv("RESOURCEY_DATABASE_PASSWORD", "secret")
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig.get_instance()
        assert cfg.database.url == url
        assert cfg.database.password.get_secret_value() == "secret"

    def test_password_spliced_into_database_url(self):
        cfg = FrameworkConfig()
        cfg.database.url = "postgresql+asyncpg://resourcey@localhost:5432/resourcey"
        cfg.database.password = SecretStr("s3cret")
        assert (
            cfg.database.database_url
            == "postgresql+asyncpg://resourcey:s3cret@localhost:5432/resourcey"
        )

    def test_reserved_characters_in_password_are_encoded(self):
        # A password with URL-reserved characters must not corrupt the URL.
        cfg = FrameworkConfig()
        cfg.database.url = "postgresql+asyncpg://resourcey@localhost:5432/resourcey"
        cfg.database.password = SecretStr("p@ss/w:rd#x")
        assert cfg.database.database_url == (
            "postgresql+asyncpg://resourcey:p%40ss%2Fw%3Ard%23x@localhost:5432/resourcey"
        )

    def test_database_url_without_password_is_verbatim(self):
        # No password configured -> the URL is returned exactly as given (an
        # inline password, if any, still works).
        cfg = FrameworkConfig()
        cfg.database.url = "sqlite+aiosqlite:///example.db"
        assert cfg.database.database_url == "sqlite+aiosqlite:///example.db"

    def test_default_database_url(self):
        cfg = FrameworkConfig()
        assert cfg.database.database_url == (
            "postgresql+asyncpg://resourcey@localhost:5432/resourcey"
        )

    def test_database_url_from_env(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_DATABASE_URL", "sqlite+aiosqlite:///from_env.db")
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig.get_instance()
        assert cfg.database.database_url == "sqlite+aiosqlite:///from_env.db"

    def test_database_url_is_property_not_field(self):
        # database_url is a plain @property, not a model field — from_env/to_env ignore it.
        assert "database_url" not in DbConfig.model_fields

    def test_embedded_url_selects_embedded_client(self):
        cfg = FrameworkConfig()
        cfg.database.url = "embedded://message_board"
        assert cfg.database.is_embedded is True
        assert cfg.database.mongo_database_name("fallback") == "message_board"

    def test_bare_embedded_marker_uses_default_database(self):
        cfg = FrameworkConfig()
        cfg.database.url = "embedded"
        assert cfg.database.is_embedded is True
        assert cfg.database.mongo_database_name("fallback") == "fallback"

    def test_mongodb_url_database_name_from_path(self):
        cfg = FrameworkConfig()
        # Multi-host URLs must parse without a connection or DNS lookup.
        cfg.database.url = "mongodb://user@h1:27017,h2:27017/message_board?replicaSet=rs0"
        assert cfg.database.is_embedded is False
        assert cfg.database.mongo_database_name("fallback") == "message_board"

    def test_mongodb_url_without_database_uses_default(self):
        cfg = FrameworkConfig()
        cfg.database.url = "mongodb://localhost:27017"
        assert cfg.database.mongo_database_name("fallback") == "fallback"

    def test_mongo_password_is_none_when_unset(self):
        assert FrameworkConfig().database.mongo_password is None

    def test_mongo_password_returns_plaintext(self):
        cfg = FrameworkConfig()
        cfg.database.password = SecretStr("s3cret")
        assert cfg.database.mongo_password == "s3cret"

    def test_cors_origins_json_array(self, monkeypatch):
        monkeypatch.setenv(
            "RESOURCEY_CORS_ORIGINS", '["https://a.example.com","https://b.example.com"]'
        )
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig.get_instance()
        assert cfg.cors_origins == ["https://a.example.com", "https://b.example.com"]

    def test_cors_origins_sequential(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("RESOURCEY_CORS_ORIGINS_0", "https://a.example.com")
        monkeypatch.setenv("RESOURCEY_CORS_ORIGINS_1", "https://b.example.com")
        FrameworkConfig.clear_instance_cache()
        cfg = FrameworkConfig.get_instance()
        assert cfg.cors_origins == ["https://a.example.com", "https://b.example.com"]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_invalid_int_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_AUTH_IDP_ACCESS_TOKEN_EXPIRES_IN", "not-an-int")
        FrameworkConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="RESOURCEY"):
            FrameworkConfig.get_instance()

    def test_out_of_range_int_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_AUTH_IDP_ACCESS_TOKEN_EXPIRES_IN", "0")
        FrameworkConfig.clear_instance_cache()
        with pytest.raises(ResourceyConfigError, match="RESOURCEY"):
            FrameworkConfig.get_instance()

    def test_config_error_is_resourcey_error(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_AUTH_IDP_ACCESS_TOKEN_EXPIRES_IN", "not-an-int")
        FrameworkConfig.clear_instance_cache()
        with pytest.raises(ResourceyError):
            FrameworkConfig.get_instance()


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


class _LazyConfig(BaseConfig):
    """Config with a lazy polymorphic field (prefix ``LAZYCFG``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "LAZYCFG"

    pet: ClassVar[Animal] = LazyField()


class _LazyConfigWithDefault(BaseConfig):
    """Lazy field with a default, so an unset var resolves instead of raising."""

    @classmethod
    def get_prefix(cls) -> str:
        return "LAZYCFG"

    pet: ClassVar[Animal] = LazyField(default=Cat)


class TestLazyField:
    def test_not_resolved_at_construction(self, monkeypatch):
        # No env vars set — constructing the config must not import the subclass.
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = _LazyConfig()
        # Genuine laziness: instance.__dict__ is empty after construction.
        assert "_lazy_pet" not in instance.__dict__

    def test_resolves_on_first_access(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_MEOW", "mrow")
        instance = _LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Cat)
        assert pet.meow == "mrow"

    def test_cached_on_instance(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        instance = _LazyConfig()
        first = instance.pet
        second = instance.pet
        assert first is second
        assert "_lazy_pet" in instance.__dict__

    def test_missing_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = _LazyConfig()
        with pytest.raises(ResourceyConfigError, match="PET_CLASS"):
            _ = instance.pet

    def test_empty_class_var_raises_instead_of_falling_back(self, monkeypatch):
        """A set-but-empty var is a misconfiguration, not "use the default".

        Regression: an empty ``{NAME}_CLASS`` used to be treated as unset and
        silently resolve to the default (here, no default -> raise; for
        ``dependency_builder`` -> a no-auth builder).
        """
        monkeypatch.setenv("PET_CLASS", "")
        instance = _LazyConfig()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_empty_class_var_does_not_use_default(self, monkeypatch):
        """Even with a default configured, an empty var raises rather than defaulting."""
        monkeypatch.setenv("PET_CLASS", "   ")
        instance = _LazyConfigWithDefault()
        with pytest.raises(ResourceyConfigError, match="set but empty"):
            _ = instance.pet

    def test_unset_class_var_uses_default(self, monkeypatch):
        """An unset var (no key at all) still falls back to the default."""
        monkeypatch.delenv("PET_CLASS", raising=False)
        instance = _LazyConfigWithDefault()
        assert isinstance(instance.pet, Cat)

    def test_non_subclass_class_var_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{NotAnAnimal.__module__}.NotAnAnimal")
        instance = _LazyConfig()
        with pytest.raises(ResourceyConfigError, match="Animal"):
            _ = instance.pet

    def test_dog_subclass(self, monkeypatch):
        monkeypatch.setenv("PET_CLASS", f"{Dog.__module__}.Dog")
        monkeypatch.setenv("PET_BARK", "loud")
        instance = _LazyConfig()
        pet = instance.pet
        assert isinstance(pet, Dog)
        assert pet.bark == "loud"

    def test_invalid_field_value_raises_config_error(self, monkeypatch):
        # Cat.volume is int; set a non-int value so from_env fails.
        monkeypatch.setenv("PET_CLASS", f"{Cat.__module__}.Cat")
        monkeypatch.setenv("PET_VOLUME", "not-an-int")
        instance = _LazyConfig()
        with pytest.raises(ResourceyConfigError):
            _ = instance.pet

    def test_access_from_class_returns_descriptor(self):
        # Accessing the lazy field on the class (not an instance) returns the descriptor itself.
        assert isinstance(_LazyConfig.pet, LazyField)

    def test_non_qualified_class_name_raises_config_error(self, monkeypatch):
        # A bare class name with no module path cannot be imported.
        monkeypatch.setenv("PET_CLASS", "BareClassName")
        instance = _LazyConfig()
        with pytest.raises(ResourceyConfigError, match="fully-qualified"):
            _ = instance.pet


# ---------------------------------------------------------------------------
# _unwrap_classvar
# ---------------------------------------------------------------------------


class TestUnwrapClassvar:
    def test_strips_classvar(self):
        from typing import ClassVar

        assert _unwrap_classvar(ClassVar[Animal]) is Animal

    def test_passes_through_non_classvar(self):
        assert _unwrap_classvar(Animal) is Animal


# ---------------------------------------------------------------------------
# No openhands import in new config modules
# ---------------------------------------------------------------------------


class TestNoOpenhandsImport:
    @pytest.mark.parametrize(
        "module",
        [
            "resourcey.config.config_base",
            "resourcey.config.config_loader",
            "resourcey.config.lazy_field",
            "resourcey.config.config_framework",
        ],
    )
    def test_no_openhands_import(self, module):
        import importlib

        mod = importlib.import_module(module)
        source = Path(mod.__file__).read_text()
        assert "openhands" not in source.lower()
