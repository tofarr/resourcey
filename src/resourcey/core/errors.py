"""Framework exception types.

``ResourceyError`` is the base for every error raised by the framework, so
callers can catch all resourcey failures with a single ``except``. The
service-level classes (``ServiceError``, ``NotFoundError``, ``ForbiddenError``)
also derive from it but stay with the code that raises them (see
``core/service.py``).

This module is part of the ``core`` bottom layer: it imports no other project
package.
"""


class ResourceyError(Exception):
    """Base class for all framework errors."""


class ResourceyConfigError(ResourceyError):
    """A configuration could not be built or parsed from the environment."""


class InvalidInputError(ResourceyError):
    """A request named something the resource does not support.

    Raised when a ``field__op`` query parameter is not part of the resource's
    query surface. The transport maps it to ``400``.
    """


class UnsupportedFilterError(ResourceyError):
    """A search filter could not be pushed down to the storage backend.

    Filtering is all-or-nothing per filter: when the backend cannot translate a
    filter into its native query (and the resource has not opted into an
    in-memory iteration fallback) it raises this rather than silently scanning
    the whole table. The transport maps it to ``501``.
    """


class ConflictError(ResourceyError):
    """A write collided with an existing record (a duplicate key).

    The storage-neutral counterpart of a driver's duplicate-key error: a backend
    whose driver raises a type the transport does not know translates it here, so
    the envelope can map it to ``409`` without importing that driver.
    """
