"""Framework exception types for ``v2``.

``ResourceyError`` is the base for every error raised by the framework, so
callers can catch all resourcey failures with a single ``except``.
``ResourceyConfigError`` covers config build/parse failures — a bad integer, a
missing required variable, a malformed ``_CLASS`` value, and an unresolvable
import path in a *list* lazy field. The single-class lazy field
(``LazyField._resolve``) is the one gap: an unresolvable ``{NAME}_CLASS`` path
surfaces the underlying ``ModuleNotFoundError`` / ``AttributeError`` directly
rather than being mapped here.

This is deliberately just those two classes: the broader service-level error
hierarchy (``ServiceError``, ``NotFoundError``, …) is tracked separately and
stays with the code that raises it (see ``v2/core/service.py``).

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""


class ResourceyError(Exception):
    """Base class for all framework errors."""


class ResourceyConfigError(ResourceyError):
    """A configuration could not be built or parsed from the environment."""
