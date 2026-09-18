"""Tests for the MISSING sentinel and the custom JSON-schema generator."""

import copy
import dataclasses
from typing import Annotated

from pydantic import BaseModel, Field, TypeAdapter
from pydantic_core import PydanticUndefined

from resourcey.resource.missing import MISSING, MissingJsonSchema


def test_missing_is_singleton():
    assert MISSING is _new_missing()


def _new_missing():
    from resourcey.resource.missing import _Missing

    return _Missing()


def test_missing_repr_and_truthiness():
    assert repr(MISSING) == "MISSING"
    assert not bool(MISSING)


def test_missing_copy_returns_same_singleton():
    assert copy.copy(MISSING) is MISSING
    assert copy.deepcopy(MISSING) is MISSING


def test_missing_distinct_from_dataclasses_and_pydantic():
    assert MISSING is not dataclasses.MISSING
    assert MISSING is not PydanticUndefined
    assert MISSING != dataclasses.MISSING


def test_missing_field_default_is_missing_and_not_validated():
    class M(BaseModel):
        a: int
        b: int | None = Field(default=MISSING, validate_default=False)

    m = M(a=1)
    assert m.b is MISSING
    # Providing a real value still validates normally.
    assert M(a=1, b=2).b == 2


def test_missing_excluded_from_json_schema_without_warning():
    class M(BaseModel):
        a: int
        b: int | None = Field(default=MISSING, validate_default=False)

    # Under filterwarnings=["error"] a PydanticJsonSchemaWarning would raise.
    schema = M.model_json_schema(schema_generator=MissingJsonSchema)
    assert "default" not in schema["properties"]["b"]
    # b is optional (not required) but present.
    assert schema["required"] == ["a"]
    assert "b" in schema["properties"]


def test_missing_with_annotated_metadata_still_strips():
    class M(BaseModel):
        a: int
        b: Annotated[int | None, Field(default=MISSING, validate_default=False)]

    schema = M.model_json_schema(schema_generator=MissingJsonSchema)
    assert "default" not in schema["properties"]["b"]


def test_real_defaults_still_serialized_in_schema():
    class M(BaseModel):
        a: int = 5

    schema = M.model_json_schema(schema_generator=MissingJsonSchema)
    assert schema["properties"]["a"]["default"] == 5


def test_type_adapter_uses_missing_generator():
    class M(BaseModel):
        a: int
        b: int | None = Field(default=MISSING, validate_default=False)

    adapter = TypeAdapter(M)
    schema = adapter.json_schema(schema_generator=MissingJsonSchema)
    assert "default" not in schema["properties"]["b"]
