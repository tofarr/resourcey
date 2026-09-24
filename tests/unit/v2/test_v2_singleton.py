"""Tests for the ``v2`` :class:`Singleton` mixin (issue #95).

Covers identity, first-construction-wins (``__init__`` runs once), per-class
caching across a base/subclass split, composition with Pydantic ``BaseModel``
(no phantom fields, ``model_validate`` routes to the cached instance) and with
:class:`~resourcey.v2.util.models.DiscriminatedUnionMixin` (one singleton per
kind), thread-safe creation, the retry-after-failure path, and
``clear_singleton_cache``.

The discriminated-union bases live at module scope because the mixin rejects
local classes when resolving kinds.
"""

from __future__ import annotations

import threading
from abc import ABC

import pytest
from pydantic import BaseModel

from resourcey.v2.util.models import DiscriminatedUnionMixin
from resourcey.v2.util.singleton import Singleton

# ---------------------------------------------------------------------------
# Module-level declarations
# ---------------------------------------------------------------------------


class PlainService(Singleton):
    """A plain (non-pydantic) singleton with no custom ``__init__``."""

    label: str = "default"


class ConfiguredService(Singleton):
    """A plain singleton whose own ``__init__`` sets state."""

    def __init__(self, config: str) -> None:
        self.config = config


class SuperCallingService(Singleton):
    """A plain singleton whose ``__init__`` reaches ``super().__init__``."""

    def __init__(self, config: str) -> None:
        super().__init__()
        self.config = config


class PydanticSingleton(Singleton, BaseModel):
    """Pydantic model with no custom ``__init__``."""

    x: int = 1


class PydanticConfiguredSingleton(Singleton, BaseModel):
    """Pydantic model with a custom ``__init__`` that enriches the instance."""

    x: int = 1

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        object.__setattr__(self, "note", f"built-{self.x}")


class RaisingSingleton(Singleton):
    """Fails the first construction; must stay retryable."""

    attempts = 0

    def __init__(self, *, fail: bool = True) -> None:
        type(self).attempts += 1
        if fail:
            raise RuntimeError("boom")


class Animal(Singleton, DiscriminatedUnionMixin, ABC):
    pass


class Cat(Animal):
    meow_volume: int = 5


class Dog(Animal):
    bark_pitch: str = "low"


class PydanticSubclass(PydanticSingleton):
    """A subclass must keep its own, independent singleton."""

    y: int = 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_singletons():
    """Drop every module-level cache between tests so they stay hermetic."""
    classes = (
        PlainService,
        ConfiguredService,
        SuperCallingService,
        PydanticSingleton,
        PydanticConfiguredSingleton,
        RaisingSingleton,
        Animal,
        Cat,
        Dog,
        PydanticSubclass,
    )
    for cls in classes:
        cls.clear_singleton_cache()
    RaisingSingleton.attempts = 0
    yield
    for cls in classes:
        cls.clear_singleton_cache()


# ---------------------------------------------------------------------------
# Identity / first-construction-wins
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_repeated_construction_returns_same_instance(self):
        assert PlainService() is PlainService()

    def test_first_construction_wins(self):
        first = PlainService()
        first.label = "mutated"
        second = PlainService()
        assert second is first
        assert second.label == "mutated"

    def test_plain_custom_init_runs_once(self):
        first = ConfiguredService("alpha")
        second = ConfiguredService("beta")
        assert first is second
        assert second.config == "alpha"

    def test_custom_init_calling_super_still_runs_once(self):
        first = SuperCallingService("alpha")
        second = SuperCallingService("beta")
        assert first is second
        assert second.config == "alpha"


# ---------------------------------------------------------------------------
# Per-class caching
# ---------------------------------------------------------------------------


class TestPerClassCaching:
    def test_base_and_subclass_are_independent(self):
        base = PydanticSingleton(x=5)
        child = PydanticSubclass(y=9)
        assert child is not base
        assert child.x == 1
        assert child.y == 9
        assert base.x == 5

    def test_each_subclass_instantiates_once(self):
        assert PydanticSingleton() is PydanticSingleton()
        assert PydanticSubclass() is PydanticSubclass()
        assert PydanticSingleton() is not PydanticSubclass()


# ---------------------------------------------------------------------------
# Pydantic composition
# ---------------------------------------------------------------------------


class TestPydanticComposition:
    def test_identity_holds(self):
        assert PydanticSingleton(x=5) is PydanticSingleton(x=9)

    def test_construct_adds_no_field(self):
        PydanticSingleton(x=5)
        assert "_singleton_instance" not in PydanticSingleton.model_fields
        assert set(PydanticSingleton.model_fields) == {"x"}

    def test_first_value_wins_and_dump_reflects_it(self):
        first = PydanticSingleton(x=5)
        PydanticSingleton(x=9)
        assert first.x == 5
        assert first.model_dump() == {"x": 5}

    def test_model_validate_returns_cached_instance(self):
        first = PydanticSingleton(x=5)
        validated = PydanticSingleton.model_validate({"x": 42})
        assert validated is first
        assert first.x == 5

    def test_custom_pydantic_init_runs_once(self):
        first = PydanticConfiguredSingleton(x=4)
        second = PydanticConfiguredSingleton(x=99)
        assert first is second
        assert first.x == 4
        assert first.note == "built-4"

    def test_custom_pydantic_init_survives_validation(self):
        first = PydanticConfiguredSingleton(x=4)
        validated = PydanticConfiguredSingleton.model_validate({"x": 50})
        assert validated is first
        assert first.x == 4
        assert first.note == "built-4"


# ---------------------------------------------------------------------------
# DiscriminatedUnionMixin composition
# ---------------------------------------------------------------------------


class TestDiscriminatedUnionComposition:
    def test_identity_per_kind(self):
        cat = Cat(meow_volume=3)
        assert Cat(meow_volume=8) is cat
        assert cat.meow_volume == 3

    def test_sibling_kinds_are_distinct_singletons(self):
        cat = Cat()
        dog = Dog()
        assert cat is not dog
        assert cat.kind == "Cat"
        assert dog.kind == "Dog"

    def test_validate_routes_to_cached_instance(self):
        cat = Cat(meow_volume=3)
        validated = Animal.model_validate({"kind": "Cat", "meow_volume": 11})
        assert validated is cat
        assert cat.meow_volume == 3

    def test_kind_serializes(self):
        assert Cat(meow_volume=3).model_dump() == {"meow_volume": 3, "kind": "Cat"}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_construction_yields_one_instance(self):
        created: list[PydanticSingleton] = []
        barrier = threading.Barrier(20)

        def make() -> None:
            barrier.wait()
            created.append(PydanticSingleton(x=5))

        threads = [threading.Thread(target=make) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(created) == 20
        assert len({id(instance) for instance in created}) == 1


# ---------------------------------------------------------------------------
# Failure / retry and cache clearing
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_failed_init_stays_retryable(self):
        with pytest.raises(RuntimeError, match="boom"):
            RaisingSingleton(fail=True)
        recovered = RaisingSingleton(fail=False)
        assert recovered is RaisingSingleton()
        assert RaisingSingleton.attempts == 2

    def test_clear_singleton_cache_forces_a_fresh_build(self):
        first = PlainService()
        PlainService.clear_singleton_cache()
        assert PlainService() is not first

    def test_clear_is_per_class(self):
        base = PydanticSingleton()
        child = PydanticSubclass()
        PydanticSingleton.clear_singleton_cache()
        assert PydanticSubclass() is child
        assert PydanticSingleton() is not base

    def test_clear_is_a_noop_when_never_built(self):
        # Exercises both attribute-absent branches of clear_singleton_cache.
        PydanticSingleton.clear_singleton_cache()
        assert PydanticSingleton() is PydanticSingleton()
