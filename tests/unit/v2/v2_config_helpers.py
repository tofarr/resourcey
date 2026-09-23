"""Module-level config classes for the ``v2`` config tests.

``get_instance()`` reads the environment by prefix, so these classes must live
at module scope for the env-var names to be predictable. Deliberately
v2-named so they cannot collide with the v1 helpers that remain in
``tests/unit/`` and are imported by bare name.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from v2_config_lazy_helpers import Animal, Cat

from resourcey.v2.config.config_base import BaseConfig
from resourcey.v2.config.lazy_field import LazyField
from resourcey.v2.util.models import DiscriminatedUnionMixin


class V2FrameworkLikeConfig(BaseConfig):
    """A base config, standing in for a framework-level config."""

    @classmethod
    def get_prefix(cls) -> str:
        return "V2CFG"

    base_url: str = Field(default="http://localhost:8000", description="Public base URL")


class V2AppLikeConfig(V2FrameworkLikeConfig):
    """An app config extending it — the composition-root case from the issue."""

    feature: str = "off"


class V2RequiredConfig(BaseConfig):
    """A config with a required field, for the build-failure mapping."""

    @classmethod
    def get_prefix(cls) -> str:
        return "V2REQ"

    name: str


class V2IntConfig(BaseConfig):
    """A config whose field parses as an int, for the parse-failure mapping."""

    @classmethod
    def get_prefix(cls) -> str:
        return "V2INT"

    port: int = 8000


class V2LazyConfig(BaseConfig):
    """Config with a lazy polymorphic field (prefix ``V2LAZY``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "V2LAZY"

    pet: ClassVar[Animal] = LazyField()


class V2LazyDefaultConfig(BaseConfig):
    """Lazy field with a default, so an unset var resolves instead of raising.

    ``Cat`` is a zero-arg callable, so each config instance gets its own
    ``Cat()`` rather than sharing one mutable default.
    """

    @classmethod
    def get_prefix(cls) -> str:
        return "V2LAZY"

    pet: ClassVar[Animal] = LazyField(default=Cat)


class V2LazyListConfig(BaseConfig):
    """Config with a lazy ``list[type[...]]`` field (prefix ``V2LAZYLIST``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "V2LAZYLIST"

    pets: ClassVar[list[type[DiscriminatedUnionMixin]]] = LazyField()
