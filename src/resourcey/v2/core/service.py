"""``Service`` — the storage-agnostic action contract — plus ``Action`` and friends.

A service exposes the standard resource actions (create, read, update, delete,
search, count, batch_read, batch_edit) as async methods. It is generic over the
DTO type it serves, so it takes DTOs in and declares DTOs as its return types.

The service is the **async context manager**: it owns its storage lifetime, not
the resource. Two storage strategies are both expressible and core privileges
neither (see :class:`~resourcey.v2.core.resource.Resource`):

* session-per-service — a caller supplies the storage via ``ctx`` and owns
  commit/close;
* session-per-operation — the service opens and closes its own storage.

The base class holds no storage. Concrete services override the actions they
support; the raising defaults are a safety net so a misconfigured route
surfaces clearly rather than silently doing nothing.

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Action(enum.StrEnum):
    """The standard resource actions.

    Member *names* (lowercased) match the :class:`Service` method names exactly
    (``CREATE`` -> ``create``, ``BATCH_READ`` -> ``batch_read``) so a route
    builder can derive a method name from an :class:`Action` directly. Being a
    ``StrEnum``, ``Action("create") in supported`` works for a correctly
    spelled string, which is why a startup assertion — not mypy — is what
    catches a typo in a dynamically built action set.
    """

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    SEARCH = "search"
    COUNT = "count"
    BATCH_READ = "batch_read"
    BATCH_EDIT = "batch_edit"


# Call-scoped context key under which a service caches the storage it opened.
# A plain ``object()`` so two independent modules cannot collide on a string;
# module scope so every resource in the process shares the one key.
STORAGE_KEY: Any = object()


class ServiceError(Exception):
    """A service is misconfigured (e.g. used before it was entered)."""


class NotFoundError(Exception):
    """A requested entity does not exist (the transport layer maps it to 404)."""

    def __init__(self, id: Any) -> None:  # noqa: A002
        super().__init__(f"No entity with id {id!r}")
        self.id = id


class CacheStrategy:
    """A cache policy placeholder: the ``v2`` core names the concept only.

    Concrete strategies live in ``resourcey.v2.cache``; ``v2/core`` deliberately
    does not depend on them. A migrated resource supplies its own strategy and
    the transport layer interprets it. The two methods below are the whole
    transport-facing contract a concrete strategy must satisfy: a header for a
    list of read models, and one for a bare count. Both return ``None`` here
    (no caching), and both are typed ``Any`` so ``core`` stays free of the
    concrete ``CacheHeader`` type.
    """

    def get_cache_header(self, models: list[Any], *, context: dict[str, Any] | None = None) -> Any:
        """Compute a transport-specific cache header for ``models`` (default: none)."""
        return None

    def count_cache_header(self, count: int, filters: Any = None) -> Any:
        """Compute a cache header for a bare ``count`` result (default: none)."""
        return None


@dataclass
class Page(Generic[T]):
    """A page of cursor-paginated search results.

    ``next_cursor`` is an opaque keyset cursor pointing at the last item of
    this page; pass it as the ``cursor`` on the next call to fetch the
    following page. It is ``None`` when this page is the last.
    """

    items: list[T] = field(default_factory=list)
    limit: int = 20
    next_cursor: str | None = None


class Service(Generic[T]):
    """The storage-agnostic service contract, generic over the DTO type ``T``.

    The service is its own async context manager: it owns the lifetime of any
    storage it opens. An action method called before ``__aenter__`` raises
    :class:`ServiceError` rather than operating on half-initialised state, and
    re-entering an already-entered service raises too.

    Subclasses override the actions the resource exposes. They may override
    ``__aenter__`` / ``__aexit__`` to open and close storage, but must call
    ``super()`` so the entered-guard stays intact.
    """

    def __init__(self) -> None:
        self._entered = False

    # ------------------------------------------------------------------
    # Lifecycle (the service is the async context manager)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Service[T]:
        """Mark the service entered and return it."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Mark the service exited."""
        self._entered = False

    @property
    def entered(self) -> bool:
        """Whether the service is currently inside its ``async with`` block."""
        return self._entered

    def _require_entered(self) -> None:
        """Raise a clear error when an action is used outside its ``async with``."""
        if not self._entered:
            raise ServiceError(
                f"{type(self).__name__} was used before entering it; call it inside "
                "'async with service:' so it can open (and later close) its storage."
            )

    # ------------------------------------------------------------------
    # Standard actions (raising defaults — override the supported ones)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        raise NotImplementedError

    async def read(self, id: Any) -> T:  # noqa: A002
        self._require_entered()
        raise NotImplementedError

    async def update(self, payload: T) -> T:
        """Apply an update DTO (which carries its own identifier); return the DTO."""
        self._require_entered()
        raise NotImplementedError

    async def delete(self, id: Any) -> None:  # noqa: A002
        self._require_entered()
        raise NotImplementedError

    async def search(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: Any = None,
    ) -> Page[T]:
        self._require_entered()
        raise NotImplementedError

    async def count(self, *, filters: Any = None) -> int:
        self._require_entered()
        raise NotImplementedError

    async def batch_read(self, ids: list[Any]) -> list[T | None]:
        self._require_entered()
        raise NotImplementedError

    async def batch_edit(self, edits: list[T]) -> list[T | None]:
        """Apply a list of update DTOs, each carrying its own identifier."""
        self._require_entered()
        raise NotImplementedError


def assert_real_actions(name: str, actions: frozenset[Any]) -> None:
    """Assert every member of a dynamically built action set is a real :class:`Action`.

    ``Action`` is a ``StrEnum``, so a correctly spelled string compares equal to
    a member and a typo silently drops a route rather than failing. This is the
    startup check that turns that silent drop into a loud error.
    """
    unknown = sorted(str(a) for a in actions if not isinstance(a, Action))
    if unknown:
        raise ServiceError(
            f"{name}.get_supported_actions() contained {unknown}; every member must be an "
            "Action (a typo would otherwise silently drop a route)."
        )
