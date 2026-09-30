"""Framework exception types.

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
and stays with the code that raises it (see ``core/service.py``).

It also holds the small **driver-conflict registry** (issue #17 prerequisite):
a backend that owns an optional driver registers its driver's integrity /
duplicate-key exception type here, so the transport maps it to the ``409``
envelope *without importing the driver*. An eager driver import on the
``http`` path would make the driver a hard dependency of every app — the very
thing that stops "optional extras" from meaning anything.

This module is part of the ``core`` bottom layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

from collections.abc import Callable

# A driver exception's message builder: given the driver exception, return the
# text for the ``conflict`` envelope. Typed against ``Exception`` so ``core``
# never names a driver type (and so the registered type satisfies FastAPI's
# ``add_exception_handler``, which accepts only ``Exception`` subclasses).
DriverConflictHandler = Callable[[Exception], str]

_driver_conflict_handlers: list[tuple[type[Exception], DriverConflictHandler]] = []


def register_driver_conflict(exc_type: type[Exception], handler: DriverConflictHandler) -> None:
    """Register a driver exception type the transport maps to ``409 conflict``.

    A backend that owns an optional driver (e.g. ``sql`` and SQLAlchemy's
    ``IntegrityError``) registers here — from the module that owns the driver, at
    import time — so ``resourcey.http`` maps the failure to the storage-neutral
    envelope without importing the driver itself. ``handler`` receives the
    exception and returns the envelope's message (typically the driver's own
    ``orig`` text).
    """
    _driver_conflict_handlers.append((exc_type, handler))


def iter_driver_conflicts() -> tuple[tuple[type[Exception], DriverConflictHandler], ...]:
    """The registered ``(exception type, message builder)`` pairs, in registration order.

    Read by the transport's error-handler registration; a snapshot (a tuple) so
    iterating it while a backend registers is safe.
    """
    return tuple(_driver_conflict_handlers)


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


class ConflictError(ResourceyError):
    """A write collided with an existing record (a duplicate key).

    The storage-neutral counterpart of a driver's duplicate-key error: a backend
    whose driver raises a type the transport does not know (e.g. pymongo's
    ``DuplicateKeyError``) translates it here, so the envelope can map it to
    ``409`` without importing that driver. ``sql`` keeps letting SQLAlchemy's
    ``IntegrityError`` reach the same ``409`` handler directly.
    """
