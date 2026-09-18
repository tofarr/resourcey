"""Module-level config classes for ``config_runtime`` tests.

``get_config()``'s env-var discovery imports the named class by its
fully-qualified name, so the classes must live at module scope (not local to a
test function) to be importable.

* ``AppConfig`` — a ``BaseConfig`` subclass with a distinguishable prefix.
* ``NotBaseConfig`` — a plain class that is NOT a ``BaseConfig`` subclass,
  for the non-subclass rejection test.
"""

from __future__ import annotations

from resourcey.config.config_base import BaseConfig


class AppConfig(BaseConfig):
    """A concrete app config subclassing ``BaseConfig`` (prefix ``APPCFG``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "APPCFG"

    app_name: str = "myapp"


class NotBaseConfig:
    """A class that does NOT subclass ``BaseConfig`` (for the non-subclass test)."""
