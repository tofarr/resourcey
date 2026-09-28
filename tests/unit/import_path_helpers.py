"""Module-level classes for the ``util.import_paths`` tests.

Dotted-path resolution imports a module-level attribute by fully-qualified
name, so these must live at module scope (not local to a test function).
Deliberately namespaced so they cannot collide with the other helpers
in ``tests/unit/`` and are imported by bare name.
"""

from __future__ import annotations

from pydantic import BaseModel


class PathWidget(BaseModel):
    """A resolvable class (also the non-subclass case for the base check)."""

    label: str = "widget"


class PathBase(BaseModel):
    """Base for the subclass-enforcement cases."""


class PathGadget(PathBase):
    """A subclass of :class:`PathBase`."""

    name: str = "gadget"
