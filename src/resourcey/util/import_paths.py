"""Resolve dotted import paths to objects, lazily and never at import time.

Used by config fields that carry "list of dotted paths -> list of classes"
(e.g. :attr:`FrameworkConfig.resources`). Resolution imports the named modules
on first read so importing a config module never triggers the work.
"""

from __future__ import annotations

import importlib
from typing import Any


def resolve_import_path(fqn: str) -> Any:
    """Import and return the object named by a fully-qualified dotted path.

    ``module.sub.Class`` -> the ``Class`` attribute of ``module.sub``.
    Raises :class:`ValueError` for a bare name with no module part, and
    propagates :class:`ImportError` / :class:`AttributeError` from a bad path.
    """
    module_name, _, attr_name = fqn.rpartition(".")
    if not module_name:
        raise ValueError(f"Import path {fqn!r} must be fully-qualified (module.attr)")
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def resolve_import_paths(fqns: list[str], *, base: type | None = None) -> list[Any]:
    """Resolve a list of dotted paths to objects.

    When ``base`` is given, every resolved object must be a ``type`` that is a
    subclass of ``base``; otherwise :class:`TypeError` is raised. This keeps a
    misconfigured list (a non-class, or the wrong kind of class) from being
    silently accepted.
    """
    resolved: list[Any] = []
    for fqn in fqns:
        obj = resolve_import_path(fqn)
        if base is not None and not (isinstance(obj, type) and issubclass(obj, base)):
            raise TypeError(f"Import path {fqn!r} does not resolve to a {base.__name__} subclass")
        resolved.append(obj)
    return resolved
