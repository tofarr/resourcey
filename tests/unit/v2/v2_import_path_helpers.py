"""Module-level classes for the ``v2.util.import_paths`` tests.

Dotted-path resolution imports a module-level attribute by fully-qualified
name, so these must live at module scope (not local to a test function).
Deliberately v2-named so they cannot collide with the v1 helpers that remain
in ``tests/unit/`` and are imported by bare name.
"""

from __future__ import annotations

from pydantic import BaseModel


class V2PathWidget(BaseModel):
    """A resolvable class (also the non-subclass case for the base check)."""

    label: str = "widget"


class V2PathBase(BaseModel):
    """Base for the subclass-enforcement cases."""


class V2PathGadget(V2PathBase):
    """A subclass of :class:`V2PathBase`."""

    name: str = "gadget"
