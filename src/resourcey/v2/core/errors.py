"""Framework exception types for ``v2``.

``ResourceyError`` is the base for every error raised by the framework, so
callers can catch all resourcey failures with a single ``except``.
``ResourceyConfigError`` covers config build/parse failures — a bad integer, a
missing required variable, a malformed ``_CLASS`` value, and an unresolvable
import path in a *list* lazy field. The single-class lazy field
(``LazyField._resolve``) is the one gap: an unresolvable ``{NAME}_CLASS`` path
surfaces the underlying ``ModuleNotFoundError`` / ``AttributeError`` directly
rather than being mapped here.

This module holds the framework-level classes only: ``ResourceyError`` (the
base), ``ResourceyConfigError``, and the request-shape errors
``InvalidInputError`` / ``UnsupportedFilterError``. The broader service-level
error hierarchy (``ServiceError``, ``NotFoundError``, …) is tracked separately
and stays with the code that raises it (see ``v2/core/service.py``).

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""


class ResourceyError(Exception):
    """Base class for all framework errors."""


class ResourceyConfigError(ResourceyError):
    """A configuration could not be built or parsed from the environment."""


class InvalidInputError(ResourceyError):
    """A request named something the resource does not support.

    Raised when a ``field__op`` query parameter is not part of the resource's
    query surface — an unknown field, an operator the field's type does not
    allow, or any filter parameter on a resource with no query surface. The
    transport maps it to ``400``.
    """


class UnsupportedFilterError(ResourceyError):
    """A search filter could not be pushed down to the storage backend.

    Filtering is all-or-nothing per filter: when the backend cannot translate
    a filter into its native query (and the resource has not opted into an
    in-memory iteration fallback) it raises this rather than silently scanning
    the whole table. The transport maps it to ``501``.
    """
