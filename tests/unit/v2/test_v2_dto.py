"""Tests for the ``v2/core`` DTO layer (issue #75).

Covers the acceptance criteria that do not need a running service: the
declaration conventions, the ``Missing`` type, the ``DtoField`` flags and
operation-scoped default precedence, and the six derived REST models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from resourcey.v2.core.dto import (
    DTO,
    DtoField,
    RestModels,
    request_to_dto,
    utc_now,
)
from resourcey.v2.util.missing import MISSING, Missing


class MyStoredKey(DTO):
    id: Annotated[UUID, DtoField(in_create_request=False, in_update_request=False)]
    key: Annotated[
        str, DtoField(in_read_response=False, in_search_response=False, in_update_response=False)
    ]
    description: Annotated[str | None, DtoField(default_for_create=None)]
    created_at: Annotated[
        datetime,
        DtoField(
            in_create_request=False, in_update_request=False, default_factory_for_create=utc_now
        ),
    ]


def test_missing_is_singleton_and_type_usable():
    assert Missing() is MISSING
    assert repr(MISSING) == "MISSING"
    assert not MISSING


def test_missing_copies_stay_the_singleton():
    import copy

    assert copy.copy(MISSING) is MISSING
    assert copy.deepcopy(MISSING) is MISSING


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
    assert flags.has_default_for("create") is False
    assert flags.has_default_for("update") is False


def test_default_precedence_client_then_default_then_missing():
    # explicit client value wins
    assert MyStoredKey.new(key="k", description="given").description == "given"
    # explicit None is a supplied value and survives
    assert MyStoredKey.new(key="k", description=None).description is None
    # omitted field takes the create default
    assert MyStoredKey.new(key="k").description is None
    # omitted field with no create default stays MISSING
    assert MyStoredKey.new(key="k").id is MISSING
    # a factory default is resolved
    assert MyStoredKey.new(key="k").created_at is not MISSING


def test_id_convention_not_in_create_or_update_requests():
    models = MyStoredKey.get_rest_models()
    assert "id" not in models.create_request.model_fields
    assert "id" not in models.update_request.model_fields


def test_timestamp_convention_gets_create_and_update_defaults():
    fields = MyStoredKey.get_fields()
    assert fields["created_at"].in_create_request is False
    assert fields["created_at"].default_factory_for_create is utc_now
    # created_at is never touched on update...
    assert fields["created_at"].has_default_for("update") is False

    class WithUpdate(MyStoredKey):
        updated_at: Annotated[
            datetime,
            DtoField(in_create_request=False, in_update_request=False),
        ]

    fields = WithUpdate.get_fields()
    assert fields["updated_at"].default_factory_for_create is utc_now
    # ...while updated_at is re-set on every update.
    assert fields["updated_at"].default_factory_for_update is utc_now


def test_bare_default_value_is_a_create_default():
    class M(DTO):
        id: int
        label: str = "hello"

    assert M.new().label == "hello"
    assert M.get_rest_models().create_request().label == "hello"
    # It is a create default only: an update omission leaves the field alone.
    assert M.get_rest_models().update_request(label="x").label == "x"
    assert M.get_rest_models().update_request().label is MISSING


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


def test_create_request_uses_concrete_defaults_and_requires_optionals_without_one():
    # Create optionality comes only from a declared create default: description
    # has one (None), key does not so it is required, and id is not a field at all.
    models = MyStoredKey.get_rest_models()
    req = models.create_request(key="k")
    assert req.description is None
    assert "id" not in type(req).model_fields
    with pytest.raises(ValidationError):
        models.create_request()


_token_calls: list[int] = []


def make_token() -> str:
    _token_calls.append(1)
    return f"tok-{len(_token_calls)}"


def test_create_request_carries_a_factory_default_as_default_factory():
    class M(DTO):
        id: int
        token: Annotated[str, DtoField(default_factory_for_create=make_token)]

    field = M.get_rest_models().create_request.model_fields["token"]
    assert field.default_factory is make_token
    # The factory is actually used (not silently replaced by None).
    assert M.get_rest_models().create_request().token == "tok-1"
    assert M.new().token == "tok-2"


def test_update_request_keeps_patch_semantics_with_the_sentinel():
    class M(DTO):
        id: int
        description: Annotated[str | None, DtoField(default_for_create=None)]
        code: str

    req = M.get_rest_models().update_request
    omitted = req(code="x")
    assert omitted.description is MISSING
    explicit_null = req(code="x", description=None)
    assert explicit_null.description is None
    # every update field is optional (PATCH), so an empty body validates
    assert req().code is MISSING
    # a non-nullable field still rejects null
    with pytest.raises(ValidationError):
        req(code=None)


def test_update_request_never_uses_the_annotation_for_optionality():
    # A nullable field with no create default is *required* on create, so a PATCH
    # can always tell "not specified" from "set to null".
    class M(DTO):
        id: int
        note: str | None

    with pytest.raises(ValidationError):
        M.get_rest_models().create_request()
    assert M.get_rest_models().update_request().note is MISSING


def test_response_models_are_required():
    models = MyStoredKey.get_rest_models()
    with pytest.raises(ValidationError):
        models.read_response()


def test_get_default_returns_value_or_factory_result_per_operation():
    assert MyStoredKey.get_default("description") is None
    assert isinstance(MyStoredKey.get_default("created_at"), datetime)
    assert MyStoredKey.get_default("key") is MISSING
    assert MyStoredKey.get_default("id") is MISSING
    assert MyStoredKey.get_default("created_at", "update") is MISSING


def test_request_to_dto_is_the_sanctioned_hop():
    payload = MyStoredKey.get_rest_models().update_request(key="k")
    dto = request_to_dto(MyStoredKey.get_dto_type(), payload)
    assert dto.key == "k"
    # untouched fields stay MISSING rather than becoming None
    assert dto.description is MISSING
    assert dto.created_at is MISSING

    cleared = request_to_dto(
        MyStoredKey.get_dto_type(),
        MyStoredKey.get_rest_models().update_request(key="k", description=None),
    )
    assert cleared.description is None


def test_raw_dump_of_a_partially_set_update_payload_is_unusable():
    """The ``exclude_unset`` hop is load-bearing, not stylistic (#101).

    A raw JSON dump raises on the sentinel; only ``exclude_unset=True`` carries
    just the client-supplied fields.
    """

    class M(DTO):
        id: int
        code: str
        note: str | None

    payload = M.get_rest_models().update_request(code="x")
    with pytest.raises(PydanticSerializationError):
        payload.model_dump(mode="json")
    assert payload.model_dump(exclude_unset=True) == {"code": "x"}
    # The sanctioned hop drops the untouched field entirely.
    dto = request_to_dto(M.get_dto_type(), payload)
    assert dto.note is MISSING


def test_new_and_get_fields_cover_to_dto_paths():
    # get_fields / get_dto_type round trip through a real UUID.
    key = uuid4()
    instance = MyStoredKey.get_dto_type()(id=key, key="k")
    assert instance.id == key


# ---------------------------------------------------------------------------
# id_field_name — selecting which field is the identifier
# ---------------------------------------------------------------------------


def test_id_field_name_defaults_to_id():
    class M(DTO):
        id: int
        name: str

    assert DTO.id_field_name == "id"
    assert M.id_field_name == "id"


def test_id_field_name_selects_another_field_and_applies_id_conventions():
    class Country(DTO, id_field_name="code"):
        code: str
        name: str

    assert Country.id_field_name == "code"
    # A custom identifier is a natural key the client supplies on create, but it
    # is immutable, so it never appears in an update request.
    assert Country.get_fields()["code"].in_create_request is True
    assert Country.get_fields()["code"].in_update_request is False
    assert Country.get_fields()["name"].in_create_request is True
    assert list(Country.get_rest_models().create_request.model_fields) == ["code", "name"]
    assert list(Country.get_rest_models().update_request.model_fields) == ["name"]
    assert list(Country.get_rest_models().read_response.model_fields) == ["code", "name"]


def test_conventional_id_is_excluded_from_create_requests():
    class M(DTO):
        id: int
        name: str

    assert M.get_fields()["id"].in_create_request is False
    assert M.get_fields()["id"].in_update_request is False
    assert list(M.get_rest_models().create_request.model_fields) == ["name"]


def test_id_field_name_leaves_a_declared_id_field_alone():
    class M(DTO, id_field_name="code"):
        id: int
        code: str
        name: str

    # Only ``code`` is the identifier: it is immutable but client-supplied on
    # create, while a plain ``id`` is just an ordinary field here.
    assert M.get_fields()["code"].in_create_request is True
    assert M.get_fields()["code"].in_update_request is False
    assert M.get_fields()["id"].in_create_request is True
    assert M.get_fields()["id"].in_update_request is True


def test_id_field_name_can_be_declared_in_the_class_body():
    class M(DTO):
        id_field_name = "slug"

        slug: str
        title: str

    assert M.id_field_name == "slug"
    assert M.get_fields()["slug"].in_create_request is True
    assert M.get_fields()["slug"].in_update_request is False


def test_id_field_name_is_inherited_and_overridable():
    class Base(DTO, id_field_name="code"):
        code: str
        name: str

    class Child(Base):
        population: int

    class Override(Base, id_field_name="name"):
        population: int

    assert Child.id_field_name == "code"
    assert list(Child.get_rest_models().create_request.model_fields) == [
        "code",
        "name",
        "population",
    ]
    assert Override.id_field_name == "name"
    # The override re-points the identifier; ``code`` reverts to an ordinary field.
    assert list(Override.get_rest_models().create_request.model_fields) == [
        "code",
        "name",
        "population",
    ]
    assert list(Override.get_rest_models().update_request.model_fields) == ["code", "population"]
    assert Override.get_fields()["name"].in_update_request is False
    assert Override.get_fields()["code"].in_update_request is True


def test_id_field_name_must_reference_a_declared_field():
    with pytest.raises(TypeError, match="has no such field"):

        class Bad(DTO, id_field_name="nope"):
            id: int


def test_id_field_name_must_be_a_non_empty_string():
    with pytest.raises(TypeError, match="non-empty string"):

        class Empty(DTO, id_field_name=""):
            id: int

    with pytest.raises(TypeError, match="non-empty string"):

        class NotAString(DTO, id_field_name=7):  # type: ignore[arg-type]
            id: int


def test_id_field_name_is_not_itself_a_field():
    class M(DTO, id_field_name="code"):
        code: str
        name: str

    assert list(M.get_fields()) == ["code", "name"]
    assert "id_field_name" not in M.get_dto_type().model_fields


# ---------------------------------------------------------------------------
# metadata — the free-form extra-data store on DTO and DtoField
# ---------------------------------------------------------------------------


def test_dto_metadata_defaults_to_empty_and_is_stored():
    class Plain(DTO):
        id: int

    assert Plain.metadata == {}

    class Tagged(DTO, metadata={"table": "threads", "version": 1}):
        id: int

    assert Tagged.metadata == {"table": "threads", "version": 1}


def test_dto_metadata_is_inherited_and_merged_child_wins():
    class Base(DTO, metadata={"table": "threads", "version": 1}):
        id: int

    class Child(Base, metadata={"version": 2, "extra": True}):
        title: str

    assert Child.metadata == {"table": "threads", "version": 2, "extra": True}
    # The parent's own mapping is not mutated by the subclass.
    assert Base.metadata == {"table": "threads", "version": 1}

    # Subclassing without metadata still inherits.
    class GrandChild(Child):
        body: str

    assert GrandChild.metadata == Child.metadata


def test_dto_metadata_can_be_declared_in_the_body():
    class Base(DTO, metadata={"table": "threads", "version": 1}):
        id: int

    class Child(Base):
        metadata: ClassVar[dict[str, Any]] = {"version": 2}

        title: str

    assert Child.metadata == {"table": "threads", "version": 2}
    assert Base.metadata == {"table": "threads", "version": 1}


def test_dto_metadata_holds_arbitrary_values():
    marker = object()

    class Rich(DTO, metadata={"anything": marker, "nested": {"a": [1, 2]}}):
        id: int

    assert Rich.metadata["anything"] is marker
    assert Rich.metadata["nested"] == {"a": [1, 2]}


def test_metadata_is_not_treated_as_a_dto_field():
    class Tagged(DTO, metadata={"x": 1}):
        id: int
        title: str

    assert list(Tagged.get_fields()) == ["id", "title"]
    assert "metadata" not in Tagged.get_dto_type().model_fields


def test_dto_field_metadata_defaults_to_empty_and_is_stored():
    assert DtoField().metadata == {}
    field = DtoField(in_read_response=False, metadata={"label": "Key", "ui": {"width": 10}})
    assert field.metadata == {"label": "Key", "ui": {"width": 10}}


def test_dto_field_metadata_survives_overrides_and_is_reachable_from_the_dto():
    class M(DTO):
        id: int
        title: str = DtoField(metadata={"label": "Title"})

    resolved = M.get_fields()["title"]
    assert resolved.metadata == {"label": "Title"}
    # Convention overrides must not drop metadata.
    id_config = M.get_fields()["id"]
    assert id_config.in_create_request is False
    assert id_config.metadata == {}

    overridden = DtoField(metadata={"label": "T"}).with_overrides(in_create_request=False)
    assert overridden.metadata == {"label": "T"}
    assert overridden.in_create_request is False


def test_dto_field_metadata_needs_no_copy_per_field():
    # Each instance owns its own dict, so one declaration cannot leak into another.
    a = DtoField()
    b = DtoField()
    a.metadata["k"] = 1
    assert b.metadata == {}


def test_dto_field_metadata_via_annotated_metadata():
    class M(DTO):
        id: int
        secret: Annotated[str, DtoField(metadata={"sensitive": True})]

    assert M.get_fields()["secret"].metadata == {"sensitive": True}


def test_dto_field_equality_and_hash_ignore_metadata():
    # metadata is an annotation, not identity: it is excluded from comparison so
    # a difference in it does not churn derived models or hashing.
    assert DtoField(metadata={"a": 1}) == DtoField(metadata={"b": 2})
    assert hash(DtoField(metadata={"a": 1})) == hash(DtoField())
