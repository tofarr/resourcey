"""Tests for the ``v2/core`` DTO layer (issue #75).

Covers the acceptance criteria that do not need a running service: the
declaration conventions, the ``Missing`` type, the ``DtoField`` flags and
logical-default precedence, and the six derived REST models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from resourcey.v2.core.dto import (
    DTO,
    MISSING,
    DtoField,
    Missing,
    RestModels,
    utc_now,
)


class MyStoredKey(DTO):
    id: UUID
    key: str = DtoField(in_read_response=False, in_search_response=False, in_update_response=False)
    description: str | None = DtoField(logical_default_value=None)
    created_at: datetime = DtoField(in_create_request=False, logical_default_value_factory=utc_now)


def test_missing_is_singleton_and_type_usable():
    assert Missing() is MISSING
    assert repr(MISSING) == "MISSING"
    assert not MISSING


def test_missing_usable_in_annotation_and_omitted_distinct_from_none():
    class M(DTO):
        id: UUID
        description: str | None

    dto = M.get_dto_type()
    omitted = dto()
    assert omitted.id is MISSING
    assert omitted.description is MISSING

    explicit_none = dto(description=None)
    assert explicit_none.description is None
    assert explicit_none.description is not MISSING


def test_missing_serializes_without_a_sentinel_value():
    class M(DTO):
        id: UUID
        description: str | None

    dumped = M.get_dto_type()(description=None).model_dump(mode="json")
    assert dumped == {"id": None, "description": None}


def test_missing_annotation_rejects_non_sentinel_values():
    class M(DTO):
        id: UUID

    dto = M.get_dto_type()
    with pytest.raises(ValidationError):
        dto(id="not-a-uuid")
    assert dto(id=MISSING).id is MISSING


def test_every_field_is_widened_with_missing_and_defaults_to_missing():
    dto = MyStoredKey.get_dto_type()
    for name, field in dto.model_fields.items():
        assert field.default is MISSING, name
    assert MyStoredKey.get_dto_type()(key="k").id is MISSING


def test_dto_field_flags_default_to_true():
    flags = DtoField()
    assert flags.in_create_request is True
    assert flags.in_create_response is True
    assert flags.in_update_request is True
    assert flags.in_update_response is True
    assert flags.in_read_response is True
    assert flags.in_search_response is True
    assert flags.has_logical_default is False


def test_logical_default_precedence_client_then_default_then_missing():
    # explicit client value wins
    assert MyStoredKey.new(key="k", description="given").description == "given"
    # explicit None is a supplied value and survives
    assert MyStoredKey.new(key="k", description=None).description is None
    # omitted field takes the logical default
    assert MyStoredKey.new(key="k").description is None
    # omitted field with no logical default stays MISSING
    assert MyStoredKey.new(key="k").id is MISSING
    # a factory default is resolved
    assert MyStoredKey.new(key="k").created_at is not MISSING


def test_id_convention_not_in_create_or_update_requests():
    models = MyStoredKey.get_rest_models()
    assert "id" not in models.create_request.model_fields
    assert "id" not in models.update_request.model_fields


def test_timestamp_convention_gets_a_logical_default_factory():
    fields = MyStoredKey.get_fields()
    assert fields["created_at"].in_create_request is False
    assert fields["created_at"].logical_default_value_factory is utc_now


def test_bare_default_value_is_a_logical_default():
    class M(DTO):
        id: int
        label: str = "hello"

    assert M.new().label == "hello"
    assert M.get_rest_models().create_request().label == "hello"


def test_dto_field_via_annotated_metadata():
    class M(DTO):
        id: int
        secret: Annotated[str, DtoField(in_read_response=False)]

    assert "secret" not in M.get_rest_models().read_response.model_fields
    assert "secret" in M.get_rest_models().create_request.model_fields


def test_classvar_and_private_attributes_are_not_fields():
    class M(DTO):
        id: int
        kind: ClassVar[str] = "m"
        _hidden: int = 0
        visible: str

    assert set(M.get_fields()) == {"id", "visible"}


def test_inherited_fields_are_collected_in_order():
    class Base(DTO):
        id: int
        name: str

    class Child(Base):
        extra: str

    assert list(Child.get_fields()) == ["id", "name", "extra"]


def test_rest_models_are_six_derived_views():
    models = MyStoredKey.get_rest_models()
    assert isinstance(models, RestModels)
    # create request omits id and created_at (conventions), keeps key + description
    assert list(models.create_request.model_fields) == ["key", "description"]
    # create response includes the one-time-reveal key
    assert list(models.create_response.model_fields) == [
        "id",
        "key",
        "description",
        "created_at",
    ]
    # read response omits key
    assert list(models.read_response.model_fields) == ["id", "description", "created_at"]


def test_one_time_reveal_is_expressible():
    models = MyStoredKey.get_rest_models()
    assert "key" in models.create_response.model_fields
    assert "key" in models.create_request.model_fields
    assert "key" not in models.read_response.model_fields
    assert "key" not in models.update_response.model_fields


def test_request_models_use_concrete_fields_with_real_defaults():
    # The wire format never represents a sentinel: an omitted description is
    # simply absent (defaulted to None), and id is not a request field at all.
    models = MyStoredKey.get_rest_models()
    req = models.create_request(key="k")
    assert req.description is None
    assert "id" not in type(req).model_fields


def test_response_models_are_required():
    models = MyStoredKey.get_rest_models()
    with pytest.raises(ValidationError):
        models.read_response()


def test_get_logical_default_returns_value_or_factory_result():
    assert MyStoredKey.get_logical_default("description") is None
    assert isinstance(MyStoredKey.get_logical_default("created_at"), datetime)
    assert MyStoredKey.get_logical_default("key") is MISSING
    assert MyStoredKey.get_logical_default("id") is MISSING


def test_new_and_get_fields_cover_to_dto_paths():
    # get_fields / get_dto_type round trip through a real UUID.
    key = uuid4()
    instance = MyStoredKey.get_dto_type()(id=key, key="k")
    assert instance.id == key
