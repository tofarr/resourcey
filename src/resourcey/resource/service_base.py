"""``Action`` enum and ``BaseService`` - the storage-agnostic service contract.

A service exposes the standard resource actions (create, read, update, delete,
search, count, batch_read, batch_edit) as async methods whose signatures carry
**no** notion of session, user, or RBAC. Any such concepts live as instance
state on a concrete service (e.g. :class:`~resourcey.resource.service.SqlService`
holds the ``AsyncSession``). This is what makes a service trivially wrappable:
an RBAC wrapper holds an inner service plus a user and delegates, with no
signature changes (issue #40).

Capability model
----------------
The contract a service honors is its declared action set — held on the
**resource** as :attr:`~resourcey.resource.base.BaseResource.actions` (issue
#62), not on the service and not by method presence. The action methods are
concrete raisers of ``NotImplementedError`` (not ``@abstractmethod``), so a
subclass is free to implement a subset; the raisers exist only as a safety net
so a misconfigured route surfaces clearly. The route builder narrows to the
resource's ``get_supported_actions()`` and asserts it never widens beyond the
resource's ``actions``.

``Action`` member names match the service method names exactly so the route
builder can derive a method name from an :class:`Action` directly.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

    from resourcey.cache.cache_header import CacheHeader
    from resourcey.util.search_filter import SearchFilter


class Action(enum.StrEnum):
    """The standard resource actions.

    Member *names* (lowercased) match the :class:`BaseService` method names
    exactly (``CREATE`` -> ``create``, ``BATCH_READ`` -> ``batch_read``) so the
    route builder can derive a method name from an :class:`Action` directly.
    """

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    SEARCH = "search"
    COUNT = "count"
    BATCH_READ = "batch_read"
    BATCH_EDIT = "batch_edit"


class BaseService:
    """The storage-agnostic service contract.

    Subclasses implement the actions they support; which actions a *resource*
    exposes over HTTP is declared on the resource (``actions`` /
    ``get_supported_actions()``, issue #62), so the service itself carries no
    action set. Methods not exposed are never called, so the raising defaults
    are a safety net, not the contract.

    The methods carry no session / user / authorize parameter: those concerns
    are instance state on a concrete service. This keeps the interface
    storage-agnostic and wrappable.
    """

    # ------------------------------------------------------------------
    # Standard actions (raising defaults - override the ones the resource
    # exposes via ``get_supported_actions()``)
    # ------------------------------------------------------------------

    async def create(self, payload: BaseModel) -> Any:
        raise NotImplementedError

    async def read(self, id: Any) -> Any:  # noqa: A002
        raise NotImplementedError

    async def update(self, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        raise NotImplementedError

    async def delete(self, id: Any) -> None:  # noqa: A002
        raise NotImplementedError

    async def search(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: SearchFilter[Any] | None = None,
    ) -> Any:
        raise NotImplementedError

    async def count(
        self,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        raise NotImplementedError

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        raise NotImplementedError

    async def batch_edit(
        self,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Cache header computation (optional; overriding is optional too)
    # ------------------------------------------------------------------

    def compute_cache_header(self, items: list[Any]) -> CacheHeader | None:
        """Resolve the resource's cache strategy and compute a header for ``items``.

        Returns the :class:`~resourcey.cache.cache_header.CacheHeader`, or
        ``None`` when the strategy yields no validators and no expiry (so the
        HTTP layer skips header setting entirely). The serialization context
        is threaded into the strategy so the ETag validates the same bytes the
        response body serializes to.
        """
        raise NotImplementedError

    def compute_count_cache_header(
        self,
        count: int,
        filters: SearchFilter[Any] | None,
    ) -> CacheHeader | None:
        """Compute a count-derived cache header for the ``count`` route."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Serialization context (optional; overrides supply an encryption context)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The pydantic serialization context used for secret fields.

        ``None`` means no context (secrets redact on dump). Concrete
        services that manage ``SecretStr`` fields override this to supply
        an ``encryption_service`` / ``expose_secrets`` context.
        """
        return None

    def _ctx(self) -> dict[str, Any] | None:
        """Alias for :meth:`serialization_context` (used by route handlers)."""
        return self.serialization_context()


class ServiceError(Exception):
    """A service is misconfigured (e.g. no session available).

    Currently reserved: no code path in the framework raises it yet (the
    session-factory guard raises :class:`ResourceyConfigError` instead). It is
    wired to a 500 handler in :func:`~resourcey.resource.routes.register_error_handlers`
    so future services can raise it for internal errors without touching the
    HTTP layer.
    """
