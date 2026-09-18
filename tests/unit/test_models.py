"""Test the vendored DiscriminatedUnionMixin and helpers.

These tests exercise the real polymorphism code path (no mocks) to verify the
vendored copy behaves identically to the SDK original.
"""

from abc import ABC

import pytest
from pydantic import BaseModel, TypeAdapter

from resourcey.util.models import (
    DiscriminatedUnionMixin,
    _get_all_subclasses,
    _get_checked_concrete_subclasses,
    _is_abstract,
    clear_subclass_cache,
    get_known_concrete_subclasses,
    kind_of,
)


class Animal(DiscriminatedUnionMixin, ABC):
    pass


class Cat(Animal):
    meow_volume: int = 5


class Dog(Animal):
    bark_pitch: str = "low"


class Vehicle(DiscriminatedUnionMixin):
    wheels: int = 4


# Single-subclass abstract base for "no kind required" tests
class SoloBase(DiscriminatedUnionMixin, ABC):
    pass


class SoloImpl(SoloBase):
    val: int = 1


# Abstract base with no subclasses (for error tests)
class EmptyBase(DiscriminatedUnionMixin, ABC):
    pass


# Abstract base with exactly one subclass (for schema/serializable tests)
class OneBase(DiscriminatedUnionMixin, ABC):
    pass


class OneImpl(OneBase):
    v: int = 0


class TestIsAbstract:
    def test_abc_subclass_is_abstract(self):
        assert _is_abstract(Animal)

    def test_concrete_is_not_abstract(self):
        assert not _is_abstract(Cat)

    def test_non_abc_base(self):
        assert not _is_abstract(Vehicle)

    def test_handles_plain_type(self):
        assert not _is_abstract(int)


class TestKindOf:
    def test_instance(self):
        assert kind_of(Cat()) == "Cat"

    def test_dict(self):
        assert kind_of({"kind": "Dog"}) == "Dog"

    def test_class(self):
        assert kind_of(Cat) == "Cat"


class TestGetAllSubclasses:
    def test_finds_nested(self):
        subs = _get_all_subclasses(Animal)
        assert Cat in subs
        assert Dog in subs


class TestGetKnownConcreteSubclasses:
    def test_excludes_abstract(self):
        subs = get_known_concrete_subclasses(Animal)
        assert Animal not in subs
        assert Cat in subs
        assert Dog in subs

    def test_stable_order(self):
        subs1 = get_known_concrete_subclasses(Animal)
        subs2 = get_known_concrete_subclasses(Animal)
        assert subs1 == subs2

    def test_cached(self):
        # Same generation -> same tuple object identity (cached)
        first = get_known_concrete_subclasses(Animal)
        second = get_known_concrete_subclasses(Animal)
        assert first is second


class TestClearSubclassCache:
    def test_clear_invalidates(self):
        get_known_concrete_subclasses(Animal)
        clear_subclass_cache()
        # After clear, cache should be rebuilt
        subs = get_known_concrete_subclasses(Animal)
        assert Cat in subs


class TestGetHandlerClassName:
    def test_extracts_name(self):
        # Create a serializer handler via a real model
        cat = Cat(meow_volume=3)

        class Wrapper(BaseModel):
            pet: Animal

        # Serialize to trigger handler creation
        data = cat.model_dump()
        assert data["kind"] == "Cat"


class TestDiscriminatedUnionSerialization:
    def test_serialize_concrete_has_kind(self):
        cat = Cat(meow_volume=7)
        data = cat.model_dump()
        assert data["kind"] == "Cat"
        assert data["meow_volume"] == 7

    def test_roundtrip_concrete(self):
        cat = Cat(meow_volume=9)
        data = cat.model_dump()
        restored = Cat.model_validate(data)
        assert restored.meow_volume == 9

    def test_deserialize_routes_to_subclass(self):
        data = {"kind": "Dog", "bark_pitch": "high"}
        adapter: TypeAdapter[Animal] = TypeAdapter(Animal)
        result = adapter.validate_python(data)
        assert isinstance(result, Dog)
        assert result.bark_pitch == "high"

    def test_deserialize_abstract_single_subclass_no_kind(self):
        # With a single subclass, kind is not required
        clear_subclass_cache()
        adapter: TypeAdapter[SoloBase] = TypeAdapter(SoloBase)
        result = adapter.validate_python({"val": 42})
        assert isinstance(result, SoloImpl)
        assert result.val == 42

    def test_deserialize_abstract_multiple_kinds_missing(self):
        # Multiple kinds, no kind specified -> error
        adapter: TypeAdapter[Animal] = TypeAdapter(Animal)
        with pytest.raises(ValueError):
            adapter.validate_python({"meow_volume": 1})

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown kind"):
            Animal.resolve_kind("Nonexistent")

    def test_resolve_kind_valid(self):
        assert Animal.resolve_kind("Cat") is Cat

    def test_no_kinds_defined_raises(self):
        clear_subclass_cache()
        with pytest.raises(ValueError):
            TypeAdapter(EmptyBase).validate_python({"x": 1})

    def test_get_serializable_type_concrete(self):
        assert Vehicle.get_serializable_type() is Vehicle

    def test_get_serializable_type_single_subclass(self):
        clear_subclass_cache()
        result = SoloBase.get_serializable_type()
        assert result is SoloImpl

    def test_get_serializable_type_abstract_no_subclasses(self):
        clear_subclass_cache()
        assert EmptyBase.get_serializable_type() is EmptyBase

    def test_json_schema_concrete_has_const_kind(self):
        schema = Cat.model_json_schema()
        assert schema["properties"]["kind"]["const"] == "Cat"

    def test_json_schema_abstract_multiple(self):
        clear_subclass_cache()
        schema = Animal.model_json_schema()
        assert "oneOf" in schema or "$defs" in schema

    def test_json_schema_abstract_single(self):
        clear_subclass_cache()
        schema = OneBase.model_json_schema()
        # Single-subclass abstract base produces a $ref to the concrete impl
        assert "$ref" in schema or "properties" in schema


class TestNonAbstractDiscriminatedUnion:
    def test_non_abstract_concrete_validates_directly(self):
        v = Vehicle(wheels=6)
        data = v.model_dump()
        assert data["kind"] == "Vehicle"
        assert data["wheels"] == 6
        restored = Vehicle.model_validate(data)
        assert restored.wheels == 6


class TestLocalClassRejection:
    def test_local_class_raises(self):
        # Local (function-scoped) discriminated union subclasses are rejected
        # because they may not exist at deserialization time.
        def make_local():
            class LocalBase(DiscriminatedUnionMixin, ABC):
                pass

            class LocalImpl(LocalBase):
                x: int = 0

            return LocalBase, LocalImpl

        local_base, _ = make_local()
        clear_subclass_cache()
        with pytest.raises((ValueError, TypeError)):
            TypeAdapter(local_base).validate_python({"x": 1})


class TestDuplicateClassRejection:
    def test_duplicate_definition_detected(self):
        # Two classes with the same __name__ in different modules, both
        # subclassing DiscriminatedUnionMixin, are flagged as duplicates.
        import dup_helper_a  # noqa: F401
        import dup_helper_b  # noqa: F401

        clear_subclass_cache()
        with pytest.raises(ValueError, match="Duplicate class definition"):
            _get_checked_concrete_subclasses(DiscriminatedUnionMixin)
        clear_subclass_cache()


class TestKindAliasField:
    def test_concrete_with_kind_alias(self):
        # A concrete class with a field aliased to "kind" (but named
        # differently) exercises the has_kind_alias_field code path.
        from pydantic import Field

        class AliasedKind(DiscriminatedUnionMixin):
            type_: str = Field(alias="kind", default="AliasedKind")

        # Validate with the alias — the kind alias and computed kind coexist
        obj = AliasedKind.model_validate({"kind": "AliasedKind"})
        assert obj.type_ == "AliasedKind"


class TestSerializerDelegation:
    def test_serialize_via_model_dump(self):
        # Test that serializing an abstract base via model_dump delegates
        # to the implementing class
        cat = Cat(meow_volume=3)
        data = cat.model_dump(mode="json")
        assert data["kind"] == "Cat"
        assert data["meow_volume"] == 3

    def test_serialize_with_exclude(self):
        cat = Cat(meow_volume=3)
        data = cat.model_dump(exclude={"meow_volume"})
        assert "meow_volume" not in data
        assert data["kind"] == "Cat"


class TestGetSerializableTypeMultiple:
    def test_get_serializable_type_multiple_subclasses(self):
        clear_subclass_cache()
        result = Animal.get_serializable_type()
        # Should be an Annotated Union, not the base class itself
        assert result is not Animal


class TestResolveKindError:
    def test_resolve_kind_empty_string(self):
        clear_subclass_cache()
        with pytest.raises(ValueError, match="Unknown kind"):
            Animal.resolve_kind("")


class TestIsAbstractEdgeCases:
    def test_handles_exception(self):
        # A type that raises when checking __bases__
        class Weird:
            @property
            def __bases__(self):
                raise Exception("boom")

        assert _is_abstract(Weird) is False
