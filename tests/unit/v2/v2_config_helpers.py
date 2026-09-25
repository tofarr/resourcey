"""Module-level config classes for the ``v2`` config tests.

``get_instance()`` reads the environment by the process-wide prefix (``APP``),
so these classes must live at module scope for the env-var names to be
predictable. Deliberately v2-named so they cannot collide with the v1 helpers
that remain in ``tests/unit/`` and are imported by bare name. All of them share
the one global prefix, so a field name must be unique (or type-consistent)
across the whole tree.
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

    base_url: str = Field(default="http://localhost:8000", description="Public base URL")


class V2AppLikeConfig(V2FrameworkLikeConfig):
    """An app config extending it — the composition-root case from the issue."""

    feature: str = "off"


class V2RequiredConfig(BaseConfig):
    """A config with a required field, for the build-failure mapping."""

    name: str


class V2IntConfig(BaseConfig):
    """A config whose field parses as an int, for the parse-failure mapping."""

    port: int = 8000


class V2LazyConfig(BaseConfig):
    """Config with a lazy polymorphic field."""

    pet: ClassVar[Animal] = LazyField()


class V2LazyDefaultConfig(BaseConfig):
    """Lazy field with a default, so an unset var resolves instead of raising.

    ``Cat`` is a zero-arg callable, so each config instance gets its own
    ``Cat()`` rather than sharing one mutable default.
    """

    pet: ClassVar[Animal] = LazyField(default=Cat)


class V2LazyListConfig(BaseConfig):
    """Config with a lazy ``list[type[...]]`` field."""

    pets: ClassVar[list[type[DiscriminatedUnionMixin]]] = LazyField()
