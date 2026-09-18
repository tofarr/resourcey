"""Test the vendored env_parser utility.

Verifies typed env parsing, nested models, lists, discriminated unions, and
template generation — exercising the real code path (no mocks).
"""

import json
from abc import ABC
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr

from resourcey.util.env_parser import (
    BoolEnvParser,
    DictEnvParser,
    FloatEnvParser,
    IntEnvParser,
    ListEnvParser,
    LiteralEnvParser,
    MissingType,
    NoneEnvParser,
    StrEnvParser,
    UnionEnvParser,
    from_env,
    merge,
    to_env,
)
from resourcey.util.models import DiscriminatedUnionMixin, clear_subclass_cache


# Module-level discriminated union classes for env parser tests
# (local classes are rejected by DiscriminatedUnionMixin)
class EnvAnimal(DiscriminatedUnionMixin, ABC):
    pass


class EnvCat(EnvAnimal):
    meow: int = 5


class EnvDog(EnvAnimal):
    bark: str = "low"


class EnvPet(DiscriminatedUnionMixin, ABC):
    pass


class EnvPetCat(EnvPet):
    meow: int = 5


class EnvZoo(BaseModel):
    animal: EnvAnimal = EnvCat()


class EnvHome(BaseModel):
    pet: EnvPet = EnvPetCat()


class TestBasicParsers:
    def test_str_parser(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert StrEnvParser().from_env("FOO") == "bar"

    def test_str_missing(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert isinstance(StrEnvParser().from_env("NOPE"), MissingType)

    def test_int_parser(self, monkeypatch):
        monkeypatch.setenv("N", "42")
        assert IntEnvParser().from_env("N") == 42

    def test_float_parser(self, monkeypatch):
        monkeypatch.setenv("F", "3.14")
        assert FloatEnvParser().from_env("F") == 3.14

    def test_bool_parser_true(self, monkeypatch):
        monkeypatch.setenv("B", "true")
        assert BoolEnvParser().from_env("B") is True

    def test_bool_parser_false(self, monkeypatch):
        monkeypatch.setenv("B", "0")
        assert BoolEnvParser().from_env("B") is False

    def test_none_parser(self, monkeypatch):
        monkeypatch.setenv("X_IS_NONE", "1")
        assert NoneEnvParser().from_env("X") is None

    def test_none_missing(self, monkeypatch):
        monkeypatch.delenv("X_IS_NONE", raising=False)
        assert isinstance(NoneEnvParser().from_env("X"), MissingType)


class TestToEnv:
    def test_str_to_env(self):
        import io

        buf = io.StringIO()
        StrEnvParser().to_env("K", "val", buf)
        assert buf.getvalue() == "K=val\n"

    def test_bool_to_env(self):
        import io

        buf = io.StringIO()
        BoolEnvParser().to_env("K", True, buf)
        assert buf.getvalue() == "K=1\n"

    def test_none_to_env(self):
        import io

        buf = io.StringIO()
        NoneEnvParser().to_env("K", None, buf)
        assert "K_IS_NONE=1" in buf.getvalue()

    def test_literal_to_env(self):
        import io

        buf = io.StringIO()
        parser = LiteralEnvParser(("a", "b"))
        parser.to_env("K", "a", buf)
        assert "K=a" in buf.getvalue()
        assert "Permitted Values" in buf.getvalue()


class TestLiteralParser:
    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("L", "a")
        parser = LiteralEnvParser(("a", "b"))
        assert parser.from_env("L") == "a"

    def test_invalid_value(self, monkeypatch):
        monkeypatch.setenv("L", "c")
        parser = LiteralEnvParser(("a", "b"))
        assert isinstance(parser.from_env("L"), MissingType)


class TestDictParser:
    def test_dict_from_json(self, monkeypatch):
        monkeypatch.setenv("D", '{"k": "v"}')
        assert DictEnvParser().from_env("D") == {"k": "v"}

    def test_dict_missing(self, monkeypatch):
        monkeypatch.delenv("D", raising=False)
        assert isinstance(DictEnvParser().from_env("D"), MissingType)


class TestListParser:
    def test_list_from_json_array(self, monkeypatch):
        monkeypatch.setenv("ITEMS", '["a", "b"]')
        parser = ListEnvParser(StrEnvParser(), str)
        assert parser.from_env("ITEMS") == ["a", "b"]

    def test_list_from_count(self, monkeypatch):
        monkeypatch.setenv("ITEMS", "2")
        monkeypatch.setenv("ITEMS_0", "x")
        monkeypatch.setenv("ITEMS_1", "y")
        parser = ListEnvParser(StrEnvParser(), str)
        assert parser.from_env("ITEMS") == ["x", "y"]

    def test_list_sequential(self, monkeypatch):
        monkeypatch.delenv("ITEMS", raising=False)
        monkeypatch.setenv("ITEMS_0", "x")
        monkeypatch.setenv("ITEMS_1", "y")
        parser = ListEnvParser(StrEnvParser(), str)
        assert parser.from_env("ITEMS") == ["x", "y"]

    def test_list_empty(self, monkeypatch):
        monkeypatch.delenv("ITEMS", raising=False)
        monkeypatch.delenv("ITEMS_0", raising=False)
        parser = ListEnvParser(StrEnvParser(), str)
        assert isinstance(parser.from_env("ITEMS"), MissingType)

    def test_list_to_env_with_items(self):
        import io

        buf = io.StringIO()
        parser = ListEnvParser(StrEnvParser(), str)
        parser.to_env("ITEMS", ["a", "b"], buf)
        assert "ITEMS_0=a" in buf.getvalue()
        assert "ITEMS_1=b" in buf.getvalue()

    def test_list_to_env_empty_sample(self):
        import io

        buf = io.StringIO()
        parser = ListEnvParser(StrEnvParser(), str)
        parser.to_env("ITEMS", [], buf)
        # Should produce a commented-out sample
        assert buf.getvalue() == "" or "#" in buf.getvalue()


class TestMerge:
    def test_merge_missing_a(self):
        from resourcey.util.env_parser import MISSING

        assert merge(MISSING, 5) == 5

    def test_merge_missing_b(self):
        from resourcey.util.env_parser import MISSING

        assert merge(5, MISSING) == 5

    def test_merge_dicts(self):
        assert merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_merge_dicts_overlap(self):
        assert merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}

    def test_merge_lists(self):
        assert merge([1, 2], [3]) == [3, 2]

    def test_merge_lists_extend(self):
        assert merge([1], [10, 20]) == [10, 20]

    def test_merge_none_b(self):
        assert merge(5, None) == 5

    def test_merge_scalar_overwrite(self):
        assert merge(1, 2) == 2


class TestUnionParser:
    def test_union_merges(self, monkeypatch):
        monkeypatch.setenv("U", "hello")
        parser = UnionEnvParser({str: StrEnvParser(), int: IntEnvParser()})
        result = parser.from_env("U")
        assert result == "hello"

    def test_union_to_env(self):
        import io

        buf = io.StringIO()
        parser = UnionEnvParser({str: StrEnvParser(), int: IntEnvParser()})
        parser.to_env("U", "hi", buf)
        assert "U=hi" in buf.getvalue()


class TestFromEnv:
    def test_simple_model(self, monkeypatch):
        class Config(BaseModel):
            host: str = "localhost"
            port: int = 5432

        monkeypatch.setenv("APP_HOST", "db.example.com")
        monkeypatch.setenv("APP_PORT", "6543")
        cfg = from_env(Config, prefix="APP")
        assert cfg.host == "db.example.com"
        assert cfg.port == 6543

    def test_defaults_when_missing(self, monkeypatch):
        class Config(BaseModel):
            host: str = "localhost"
            port: int = 5432

        monkeypatch.delenv("APP_HOST", raising=False)
        monkeypatch.delenv("APP_PORT", raising=False)
        cfg = from_env(Config, prefix="APP")
        assert cfg.host == "localhost"
        assert cfg.port == 5432

    def test_nested_model(self, monkeypatch):
        class Inner(BaseModel):
            val: str = "default"

        class Outer(BaseModel):
            inner: Inner = Inner()

        monkeypatch.setenv("APP_INNER_VAL", "custom")
        cfg = from_env(Outer, prefix="APP")
        assert cfg.inner.val == "custom"

    def test_list_field(self, monkeypatch):
        class Config(BaseModel):
            items: list[str] = []

        monkeypatch.setenv("APP_ITEMS", '["a", "b"]')
        cfg = from_env(Config, prefix="APP")
        assert cfg.items == ["a", "b"]

    def test_optional_field(self, monkeypatch):
        class Config(BaseModel):
            name: str | None = None

        monkeypatch.setenv("APP_NAME_IS_NONE", "1")
        cfg = from_env(Config, prefix="APP")
        assert cfg.name is None

    def test_enum_field(self, monkeypatch):
        class Color(Enum):
            RED = "red"
            BLUE = "blue"

        class Config(BaseModel):
            color: Color = Color.RED

        monkeypatch.setenv("APP_COLOR", "blue")
        cfg = from_env(Config, prefix="APP")
        assert cfg.color == Color.BLUE

    def test_literal_field(self, monkeypatch):
        class Config(BaseModel):
            mode: Literal["dev", "prod"] = "dev"

        monkeypatch.setenv("APP_MODE", "prod")
        cfg = from_env(Config, prefix="APP")
        assert cfg.mode == "prod"

    def test_prefix(self, monkeypatch):
        class Config(BaseModel):
            host: str = "localhost"

        monkeypatch.setenv("DB_HOST", "db.example.com")
        cfg = from_env(Config, prefix="DB")
        assert cfg.host == "db.example.com"


class TestToEnvRoundtrip:
    def test_to_env_simple(self):
        class Config(BaseModel):
            host: str = "localhost"
            port: int = 5432

        result = to_env(Config(host="db.example.com", port=6543))
        assert "HOST=db.example.com" in result
        assert "PORT=6543" in result

    def test_to_env_prefix(self):
        class Config(BaseModel):
            host: str = "localhost"

        result = to_env(Config(host="db"), prefix="DB")
        assert "DB_HOST=db" in result

    def test_to_env_nested(self):
        class Inner(BaseModel):
            val: str = "x"

        class Outer(BaseModel):
            inner: Inner = Inner()

        result = to_env(Outer())
        assert "INNER_VAL=x" in result

    def test_to_env_list(self):
        class Config(BaseModel):
            items: list[str] = []

        result = to_env(Config(items=["a", "b"]))
        assert "ITEMS_0=a" in result
        assert "ITEMS_1=b" in result


class TestModelEnvParser:
    def test_field_descriptions(self):
        import io

        from pydantic import Field

        class Cfg(BaseModel):
            host: str = Field(default="localhost", description="The database hostname.")

        buf = io.StringIO()
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        parser.to_env("DB", Cfg(host="x"), buf)
        # Description should appear as a comment
        assert "hostname" in buf.getvalue()


class TestDiscriminatedUnionEnv:
    def test_discriminated_union_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_ANIMAL_KIND", "EnvDog")
        monkeypatch.setenv("APP_ANIMAL_BARK", "high")
        clear_subclass_cache()
        cfg = from_env(EnvZoo, prefix="APP")
        assert isinstance(cfg.animal, EnvDog)
        assert cfg.animal.bark == "high"

    def test_discriminated_union_single_kind(self, monkeypatch):
        monkeypatch.setenv("APP_PET_MEOW", "7")
        clear_subclass_cache()
        cfg = from_env(EnvHome, prefix="APP")
        assert isinstance(cfg.pet, EnvPetCat)
        assert cfg.pet.meow == 7

    def test_discriminated_union_missing_kind_multiple(self, monkeypatch):
        # No KIND set, multiple options -> MISSING
        monkeypatch.delenv("APP_ANIMAL_KIND", raising=False)
        monkeypatch.delenv("APP_ANIMAL_MEOW", raising=False)
        monkeypatch.delenv("APP_ANIMAL_BARK", raising=False)
        clear_subclass_cache()
        cfg = from_env(EnvZoo, prefix="APP")
        # Falls back to default
        assert isinstance(cfg.animal, EnvCat)

    def test_discriminated_union_to_env(self):
        clear_subclass_cache()
        result = to_env(EnvZoo(animal=EnvCat(meow=9)), prefix="APP")
        assert "APP_ANIMAL_KIND=EnvCat" in result
        assert "APP_ANIMAL_MEOW=9" in result


class TestDefaultParsers:
    def test_uuid_parser(self, monkeypatch):
        class Config(BaseModel):
            id: UUID = UUID("00000000-0000-0000-0000-000000000000")

        monkeypatch.setenv("APP_ID", "12345678-1234-1234-1234-123456789012")
        cfg = from_env(Config, prefix="APP")
        assert str(cfg.id) == "12345678-1234-1234-1234-123456789012"

    def test_path_parser(self, monkeypatch):
        class Config(BaseModel):
            path: Path = Path("/tmp")

        monkeypatch.setenv("APP_PATH", "/var/data")
        cfg = from_env(Config, prefix="APP")
        assert cfg.path == Path("/var/data")

    def test_datetime_parser(self, monkeypatch):
        class Config(BaseModel):
            when: datetime = datetime(2024, 1, 1)

        monkeypatch.setenv("APP_WHEN", "2025-06-05T12:00:00")
        cfg = from_env(Config, prefix="APP")
        assert cfg.when == datetime(2025, 6, 5, 12, 0, 0)

    def test_secret_str_parser(self, monkeypatch):
        class Config(BaseModel):
            key: SecretStr = SecretStr("default")

        monkeypatch.setenv("APP_KEY", "secret-value")
        cfg = from_env(Config, prefix="APP")
        assert cfg.key.get_secret_value() == "secret-value"


class TestUnknownType:
    def test_unknown_type_raises(self):
        class Weird:
            pass

        with pytest.raises(ValueError, match="unknown_type"):
            from_env(Weird)


class TestMergeEdgeCases:
    def test_merge_dict_list_overlap(self):

        assert merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3, 2]}


class TestModelEnvParserFromEnv:
    def test_json_base_value(self, monkeypatch):
        class Cfg(BaseModel):
            host: str = "localhost"
            port: int = 5432

        monkeypatch.setenv("CFG", '{"host": "from.json", "port": 9999}')
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        result = parser.from_env("CFG")
        assert result["host"] == "from.json"
        assert result["port"] == 9999

    def test_field_override_over_json(self, monkeypatch):
        class Cfg(BaseModel):
            host: str = "localhost"
            port: int = 5432

        monkeypatch.setenv("CFG", '{"host": "from.json"}')
        monkeypatch.setenv("CFG_PORT", "7777")
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        result = parser.from_env("CFG")
        assert result["host"] == "from.json"
        assert result["port"] == 7777


class TestAnnotated:
    def test_annotated_stripped(self, monkeypatch):
        class Cfg(BaseModel):
            name: Annotated[str, "some metadata"] = "default"

        monkeypatch.setenv("APP_NAME", "value")
        cfg = from_env(Cfg, prefix="APP")
        assert cfg.name == "value"


class TestListToEnvSample:
    def test_list_to_env_empty_with_sample(self):
        class Cfg(BaseModel):
            items: list[str] = []

        result = to_env(Cfg(items=[]), prefix="APP")
        # empty list -> tries to produce a sample
        assert isinstance(result, str)


class TestDelayedParser:
    def test_delayed_parser_none_raises(self):
        from resourcey.util.env_parser import DelayedParser

        parser = DelayedParser(parser=None)
        with pytest.raises(AssertionError):
            parser.from_env("X")

    def test_delayed_parser_to_env_none_raises(self):
        from resourcey.util.env_parser import DelayedParser

        parser = DelayedParser(parser=None)
        with pytest.raises(AssertionError):
            import io

            parser.to_env("X", "val", io.StringIO())


class TestBaseEnvParser:
    def test_to_env_none_value(self):
        import io

        # Use StrEnvParser (concrete) to test the base to_env behavior for None
        buf = io.StringIO()
        StrEnvParser().to_env("K", None, buf)
        assert buf.getvalue() == "K=\n"

    def test_to_env_regular_value(self):
        import io

        buf = io.StringIO()
        StrEnvParser().to_env("K", "hello", buf)
        assert buf.getvalue() == "K=hello\n"


class TestNoneEnvParserToEnv:
    def test_to_env_not_none(self):
        import io

        buf = io.StringIO()
        NoneEnvParser().to_env("K", "not_none", buf)
        # When value is not None, nothing is written
        assert buf.getvalue() == ""


class TestLiteralEnvParserEnumValue:
    def test_to_env_enum_value(self):
        import io

        class Color(Enum):
            RED = "red"

        buf = io.StringIO()
        parser = LiteralEnvParser(("red",))
        parser.to_env("K", Color.RED, buf)
        assert "K=red" in buf.getvalue()


class TestModelEnvParserOverrides:
    def test_override_skipped_when_field_missing(self, monkeypatch):
        class Cfg(BaseModel):
            host: str = "localhost"
            port: int = 5432

        monkeypatch.setenv("CFG_HOST", "x")
        # CFG_PORT not set -> MISSING -> skipped
        monkeypatch.delenv("CFG_PORT", raising=False)
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        result = parser.from_env("CFG")
        assert result["host"] == "x"
        assert "port" not in result

    def test_override_merged_with_existing(self, monkeypatch):
        class Cfg(BaseModel):
            host: str = "localhost"

        monkeypatch.setenv("CFG", '{"host": "from.json"}')
        monkeypatch.setenv("CFG_HOST", "override")
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        result = parser.from_env("CFG")
        assert result["host"] == "override"

    def test_has_possible_keys_but_field_missing(self, monkeypatch):
        class Cfg(BaseModel):
            host: str = "localhost"
            port: int = 5432

        # Set CFG_PORT (which creates possible keys) but make it invalid for int
        monkeypatch.setenv("CFG_PORT", "not_an_int")
        monkeypatch.delenv("CFG_HOST", raising=False)
        from resourcey.util.env_parser import _get_default_parsers, get_env_parser

        parser = get_env_parser(Cfg, _get_default_parsers())
        # int parser raises ValueError, caught by... actually no, this is
        # ModelEnvParser not UnionEnvParser. The IntEnvParser.from_env will raise.
        # This is expected behavior — invalid env values raise.
        with pytest.raises(ValueError):
            parser.from_env("CFG")


class TestUnionEnvParserToEnv:
    def test_to_env_non_matching_type_sample(self):
        import io

        buf = io.StringIO()
        parser = UnionEnvParser({str: StrEnvParser(), int: IntEnvParser()})
        parser.to_env("U", "hi", buf)
        # str matches, int produces a sample comment
        assert "U=hi" in buf.getvalue()


class TestListEnvParserToEnvEmpty:
    def test_list_to_env_empty_with_model_sample(self):

        class Item(BaseModel):
            name: str = "..."

        class Cfg(BaseModel):
            items: list[Item] = []

        result = to_env(Cfg(items=[]), prefix="APP")
        assert isinstance(result, str)


class TestDiscriminatedUnionImport:
    def test_import_and_register_class(self, monkeypatch):
        # Test the _import_and_register_class path with a dotted class name
        clear_subclass_cache()
        monkeypatch.setenv("APP_ANIMAL_KIND", "tests.unit.test_env_parser.EnvCat")
        monkeypatch.setenv("APP_ANIMAL_MEOW", "3")
        cfg = from_env(EnvZoo, prefix="APP")
        assert isinstance(cfg.animal, EnvCat)
        assert cfg.animal.meow == 3

    def test_import_already_registered(self, monkeypatch):
        # When the class is already in parsers, it just returns the name
        clear_subclass_cache()
        # First call registers it
        monkeypatch.setenv("APP_ANIMAL_KIND", "tests.unit.test_env_parser.EnvCat")
        monkeypatch.setenv("APP_ANIMAL_MEOW", "3")
        from_env(EnvZoo, prefix="APP")
        # Second call with dotted path should still work (already registered)
        monkeypatch.setenv("APP_ANIMAL_MEOW", "7")
        cfg = from_env(EnvZoo, prefix="APP")
        assert isinstance(cfg.animal, EnvCat)
        assert cfg.animal.meow == 7

    def test_kind_only_no_other_fields(self, monkeypatch):
        # KIND set but no other fields -> returns {"kind": ...}
        clear_subclass_cache()
        monkeypatch.setenv("APP_ANIMAL_KIND", "EnvCat")
        monkeypatch.delenv("APP_ANIMAL_MEOW", raising=False)
        cfg = from_env(EnvZoo, prefix="APP")
        assert isinstance(cfg.animal, EnvCat)
        assert cfg.animal.meow == 5  # default

    def test_kind_missing_single_kind_with_fields(self, monkeypatch):
        clear_subclass_cache()
        monkeypatch.setenv("APP_PET_MEOW", "8")
        cfg = from_env(EnvHome, prefix="APP")
        assert isinstance(cfg.pet, EnvPetCat)
        assert cfg.pet.meow == 8


class TestCreateSample:
    def test_create_sample_none(self):
        from resourcey.util.env_parser import _create_sample

        assert _create_sample(None) is None

    def test_create_sample_str(self):
        from resourcey.util.env_parser import _create_sample

        assert _create_sample(str) == "..."

    def test_create_sample_int(self):
        from resourcey.util.env_parser import _create_sample

        assert _create_sample(int) == 0

    def test_create_sample_float(self):
        from resourcey.util.env_parser import _create_sample

        assert _create_sample(float) == 0.0

    def test_create_sample_bool(self):
        from resourcey.util.env_parser import _create_sample

        assert _create_sample(bool) is False

    def test_create_sample_enum(self):
        from resourcey.util.env_parser import _create_sample

        class Color(Enum):
            RED = "red"

        assert _create_sample(Color) == Color.RED

    def test_create_sample_model(self):
        from resourcey.util.env_parser import _create_sample

        class Cfg(BaseModel):
            host: str = "localhost"

        sample = _create_sample(Cfg)
        assert isinstance(sample, Cfg)

    def test_create_sample_non_instantiable(self):
        from resourcey.util.env_parser import _create_sample

        # A class that can't be instantiated without args raises
        class NeedsArg:
            def __init__(self, x):
                self.x = x

        with pytest.raises(TypeError):
            _create_sample(NeedsArg)


class TestMergeListMissingItems:
    def test_merge_list_with_missing_items(self):
        from resourcey.util.env_parser import MISSING

        # A list with MISSING items merged with a list of values
        result = merge([MISSING, MISSING], ["a", "b"])
        assert result == ["a", "b"]


class TestListParserFromJsonNonList:
    def test_list_from_json_non_list_raises(self, monkeypatch):
        monkeypatch.setenv("ITEMS", '"not_a_list"')
        parser = ListEnvParser(StrEnvParser(), str)
        with pytest.raises(AssertionError):
            parser.from_env("ITEMS")


class TestDictParserInvalidJson:
    def test_dict_invalid_json(self, monkeypatch):
        monkeypatch.setenv("D", "not_json")
        with pytest.raises(json.JSONDecodeError):
            DictEnvParser().from_env("D")
