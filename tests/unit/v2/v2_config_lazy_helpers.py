"""Module-level polymorphic classes for ``LazyField`` tests.

``LazyField`` imports the concrete subclass by fully-qualified name, so the
concrete class must live at module scope (not local to a test function) to be
importable. ``Animal`` is the abstract base; ``Cat`` and ``Dog`` are concrete
subclasses parsed via :func:`~resourcey.v2.util.env_parser.from_env`.
"""

from __future__ import annotations

from abc import ABC

from pydantic import BaseModel

from resourcey.v2.util.models import DiscriminatedUnionMixin


class Animal(DiscriminatedUnionMixin, ABC):
    """Abstract polymorphic base for lazy-field tests."""


class Cat(Animal):
    meow: str = "purr"
    volume: int = 5


class Dog(Animal):
    bark: str = "woof"


class NotAnAnimal(BaseModel):
    """A concrete class that does NOT subclass ``Animal`` (for the non-subclass test)."""

    x: int = 1
