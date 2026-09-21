"""The permission-enforcing service wrapper (issue #4).

:class:`SecuredService` wraps another :class:`BaseService` and enforces a
permission policy on every action before delegating to the inner service. It
is storage-agnostic: it depends only on
:class:`~resourcey.util.search_filter.SearchFilter` and
:class:`~resourcey.resource.service_base.BaseService` — never on a concrete
storage backend. A resource opts into security by yielding a
``SecuredService`` from ``open_service`` instead of a bare ``SqlService``;
the core resource machinery never imports this wrapper (dependency direction
is strictly inward: auth -> core, never core -> auth).

Per-action enforcement (union model — no deny-wins override):

* **CREATE** — the permission filter for ``(resource_type, CREATE)`` is
  computed; a :class:`NoneSearchFilter` (deny) raises
  :class:`ForbiddenError` (403). Otherwise the create is delegated. A policy
  may also attach create-time data (e.g. stamping ``creator_id``), handled by
  the :meth:`authorize_create` hook.
* **SEARCH / COUNT** — the permission filter is AND-combined with the
  caller's request filter via :func:`~resourcey.util.search_filter.and_filter`,
  so the principal sees only the intersection of what they requested and what
  they are permitted to see. A deny yields an empty result (not a 403) for
  collection endpoints.
* **READ** — the item is fetched; if the permission filter does not match it,
  :class:`NotFoundError` (404) is raised instead of 403, so non-permitted ids
  do not leak their existence.
* **UPDATE / DELETE** — like READ: the item must be in the permitted scope or
  :class:`NotFoundError` is raised.
* **BATCH_READ** — each requested id is resolved; non-permitted positions are
  returned as ``None`` (positional, 1:1 with the input), matching the
  batch-read contract for absent ids.
* **BATCH_EDIT** — only edits whose target id is in the permitted scope are
  applied; non-permitted positions are ``None`` (no write).

The wrapper delegates cache-header computation and the serialization context
to the inner service unchanged.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from resourcey.resource.errors import ForbiddenError, NotFoundError
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.search_filter import (
    NONE,
    NoneSearchFilter,
    SearchFilter,
    and_filter,
)

if TYPE_CHECKING:
    from resourcey.cache.cache_header import CacheHeader


PermissionResolver = Callable[
    [str, Action, uuid.UUID | None, frozenset[uuid.UUID]],
    "SearchFilter[Any] | None",
]
"""Resolve the effective permission filter for a resource + action + principal.

Returns the combined (OR) filter of every matching policy for
``(resource_type, action, user_id, groups)``, or ``None`` when no policy
applies (deny / fail-closed). The resolver is supplied by the auth package's
dependency layer; the wrapper itself stays storage-agnostic.
"""


class SecuredService(BaseService):
    """A :class:`BaseService` wrapper that enforces permission policies.

    Holds an inner service (the storage-backed service it delegates to), the
    resource type string the policies are keyed by, and a
    :data:`PermissionResolver` that reduces the principal's policies to a
    filter for a given action. The principal (``user_id`` + ``groups``) is
    instance state, set at construction from the request's auth context.

    The wrapper narrows :attr:`actions` to the inner service's actions (it
    cannot expose an action the inner service does not support).
    """

    def __init__(
        self,
        inner: BaseService,
        *,
        resource_type: str,
        resource_name: str,
        user_id: uuid.UUID | None,
        groups: frozenset[uuid.UUID],
        resolver: PermissionResolver,
    ) -> None:
        self._inner = inner
        self._resource_type = resource_type
        self._resource_name = resource_name
        self._user_id = user_id
        self._groups = groups
        self._resolver = resolver
        # Narrow to what the inner service actually supports. ``actions`` is a
        # ClassVar on the base (read off the service class by the route
        # builder); the wrapper is constructed per-request, so we shadow it on
        # the instance for runtime introspection. Mypy flags ClassVar
        # assignment via instance, hence the ignore.
        self.actions = inner.actions  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Permission filter resolution
    # ------------------------------------------------------------------

    def _permission_filter(self, action: Action) -> SearchFilter[Any]:
        """The effective permission filter for *action*, or ``NoneSearchFilter`` (deny).

        ``None`` from the resolver (no policy applied) is treated as deny
        (fail-closed), so a resource with no configured permissions is
        inaccessible rather than open.
        """
        filt = self._resolver(self._resource_type, action, self._user_id, self._groups)
        if filt is None:
            return NONE
        return filt

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: BaseModel) -> Any:
        action = Action.CREATE
        filt = self._permission_filter(action)
        if isinstance(filt, NoneSearchFilter):
            raise ForbiddenError(self._resource_name, action.value)
        self._authorize_create(payload)
        return await self._inner.create(payload)

    def _authorize_create(self, payload: BaseModel) -> None:
        """Hook to apply create-time policy side effects.

        Default: stamp ``creator_id`` from the principal when the payload has
        a ``creator_id`` field that is not set and the principal is
        authenticated. This makes :class:`~resourcey.auth.permission.CreatorPermission`
        work out of the box for resources that declare a ``creator_id`` field.
        Resources without a ``creator_id`` field are unaffected.
        """
        if self._user_id is None:
            return
        if not hasattr(payload, "creator_id"):
            return
        if getattr(payload, "creator_id", None) is None:
            with contextlib.suppress(AttributeError, ValueError):
                object.__setattr__(payload, "creator_id", self._user_id)

    async def read(self, id: Any) -> Any:  # noqa: A002
        result = await self._inner.read(id)
        if result is None:
            return None
        filt = self._permission_filter(Action.READ)
        if not filt.matches(result):
            raise NotFoundError(self._resource_name, id)
        return result

    async def update(self, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        filt = self._permission_filter(Action.UPDATE)
        # Fetch first to check scope; the inner update would otherwise write
        # before we can refuse. Reuse read so the scope check is uniform.
        existing = await self._inner.read(id)
        if existing is None:
            raise NotFoundError(self._resource_name, id)
        if not filt.matches(existing):
            raise NotFoundError(self._resource_name, id)
        return await self._inner.update(id, payload)

    async def delete(self, id: Any) -> None:  # noqa: A002
        filt = self._permission_filter(Action.DELETE)
        existing = await self._inner.read(id)
        if existing is None:
            raise NotFoundError(self._resource_name, id)
        if not filt.matches(existing):
            raise NotFoundError(self._resource_name, id)
        await self._inner.delete(id)

    async def search(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: SearchFilter[Any] | None = None,
    ) -> Any:
        perm = self._permission_filter(Action.SEARCH)
        combined = and_filter(perm, filters) if filters is not None else perm
        return await self._inner.search(
            limit=limit, cursor=cursor, sort=sort, desc=desc, filters=combined
        )

    async def count(
        self,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        perm = self._permission_filter(Action.COUNT)
        combined = and_filter(perm, filters) if filters is not None else perm
        return await self._inner.count(filters=combined)

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        results = await self._inner.batch_read(ids)
        filt = self._permission_filter(Action.BATCH_READ)
        secured: list[Any] = []
        for item in results:
            if item is None or not filt.matches(item):
                secured.append(None)
            else:
                secured.append(item)
        return secured

    async def batch_edit(
        self,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        filt = self._permission_filter(Action.BATCH_EDIT)
        permitted: list[tuple[Any, BaseModel]] = []
        permitted_flags: list[bool] = []
        for edit_id, payload in edits:
            existing = await self._inner.read(edit_id)
            if existing is not None and filt.matches(existing):
                permitted.append((edit_id, payload))
                permitted_flags.append(True)
            else:
                permitted_flags.append(False)
        if not permitted:
            # All edits denied — no write, all positions None.
            return [None] * len(edits)
        edited = await self._inner.batch_edit(permitted)
        results: list[Any] = []
        edited_iter = iter(edited)
        for allowed in permitted_flags:
            results.append(next(edited_iter) if allowed else None)
        return results

    # ------------------------------------------------------------------
    # Cache header computation (delegate to inner unchanged)
    # ------------------------------------------------------------------

    def compute_cache_header(self, items: list[Any]) -> CacheHeader | None:
        return self._inner.compute_cache_header(items)

    def compute_count_cache_header(
        self,
        count: int,
        filters: SearchFilter[Any] | None,
    ) -> CacheHeader | None:
        return self._inner.compute_count_cache_header(count, filters)
