"""The policy-enforcing service wrapper (issue #127).

:class:`AuthorizedService` wraps a resource's own service and enforces a set of
:class:`~resourcey.auth.auth_policy.Policy` objects on every action before
delegating. Its filter for an action is the **OR-combination** of every
policy's reduction (the union model), so a principal with several policies sees
the union of what each grants and a ``DenyAll`` contributes nothing; an empty
set is deny / fail-closed.
It is storage-agnostic and depends only on the lower layers — ``core`` never imports it
(dependency direction is strictly inward: ``auth`` -> ``core``), and a resource
opts in by having the dependency builder yield an ``AuthorizedService`` instead
of the bare service.

Per-action enforcement (ported verbatim from v1's ``SecuredService``):

* **create** — a denied policy raises :class:`ForbiddenError` (403).
* **read** — the row is fetched; an out-of-scope row raises
  :class:`~resourcey.core.service.NotFoundError` (404) rather than 403, so a
  non-permitted id does not leak its existence.
* **update** / **delete** — the policy filter is folded into the write's own
  ``condition`` (AND) and pushed to the backend, so scope is enforced atomically
  with the write (no read-then-write race); an absent / out-of-scope / condition
  -failed row is a 404, so a denied row is indistinguishable from an absent one.
* **search** / **count** — the policy's filter is ``and_``-combined with the
  caller's request filter and pushed down to the inner service, so a denied
  policy yields an empty page / a count of 0 rather than an error (a collection
  endpoint does not 403), and a future row-scoping policy is enforced in the
  query rather than by scanning.
* **batch_read** — positions whose row is absent or out of scope are ``None``,
  positionally aligned with the input.
* **batch_edit** — the policy filter is folded into each update / delete node's
  ``condition`` (a create is applied only when creating is permitted); every
  denied / absent / condition-failed position writes nothing and is ``None``.

The wrapper enters its inner service (like ``ViewService``), honouring "whoever
opens the storage owns its commit and close", and delegates
:meth:`serialization_context` to it, so a one-time secret reveal survives
wrapping.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from resourcey.auth.auth_policy import Policy
from resourcey.core.service import (
    DEFAULT_LIMIT,
    Action,
    Create,
    Delete,
    ForbiddenError,
    NotFoundError,
    Page,
    Service,
    Update,
)
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import NoMatchFilter, SearchFilter, and_, or_
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


class AuthorizedService(Service[T, K], Generic[T, K]):
    """A service proxy that enforces a :class:`Policy` before delegating.

    It is its own async context manager, delegating the storage lifetime to the
    inner service. The DTO type is nominal — the inner service returns DTOs and
    the transport projects them onto the resource's REST models.

    Attributes:
        policies: The authorization rules applied to every action, OR-combined
            (the union model -- ``specs/permissions.qnt``): a principal with
            several policies sees the union of what each grants, and a
            ``DenyAll`` contributes nothing. An empty list is deny / fail-closed.
        user_id: The authenticated principal's id (``None`` for anonymous),
            passed to every policy reduction so a ``Creator`` / ``Group`` policy
            can scope rows.
        id_field: The identifier field name of the served DTO, used to locate an
            existing row for the by-id actions.
        resource_name: The served resource's name, for error messages.
    """

    def __init__(
        self,
        inner: Service[T, K],
        *,
        policies: Policy | Sequence[Policy],
        id_field: str,
        resource_name: str,
        user_id: uuid.UUID | None = None,
        response_private: bool = False,
    ) -> None:
        super().__init__()
        self._inner = inner
        # Accept a bare ``Policy`` for ergonomics; normalise to a list.
        self._policies: list[Policy] = (
            [policies] if isinstance(policies, Policy) else list(policies)
        )
        self._id_field = id_field
        self._resource_name = resource_name
        self._user_id = user_id
        self._owns_inner = False
        self.set_response_private(response_private)

    # ------------------------------------------------------------------
    # Cache privacy
    # ------------------------------------------------------------------

    def response_is_private(self) -> bool:
        """Whether this service's responses are caller-scoped.

        Delegates to the inner when the inner already declares privacy, else
        reports this wrapper's own flag (set from the resolved policies). The
        wrapper's flag is the important one: an ``Owner`` policy narrows rows
        even though the inner storage is principal-agnostic.
        """
        return self._response_private or self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Lifecycle (delegated to the inner service)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AuthorizedService[T, K]:
        await super().__aenter__()
        # An inner that is already entered (e.g. the service dependency opened it
        # for the request) stays owned by whoever opened it — adopting it here
        # would double-close it. Otherwise this wrapper owns the inner lifetime.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context.

        Delegated, not the wrapper's own: the inner service decides a response's
        context (e.g. an auth key service asking for the one-time reveal), and
        the transport reads it off the service it was handed — this wrapper.
        Without this the reveal would be silently dropped.
        """
        return self._inner.serialization_context()

    # ------------------------------------------------------------------
    # Permission filter resolution
    # ------------------------------------------------------------------

    async def _permission_filter(self, action: Action) -> SearchFilter[Any]:
        """The OR-combination of every policy's filter for ``action``.

        An empty policy list -- or a list whose reductions all deny -- yields a
        ``NoMatchFilter`` (deny / fail-closed). A ``NoMatchFilter`` child is the
        identity of the union, so a ``DenyAll`` policy never suppresses another
        policy's grant (the union model, ``specs/permissions.qnt``).
        """
        reductions = [
            await policy.to_search_filter(self._user_id, action) for policy in self._policies
        ]
        return or_(*reductions)

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        filt = await self._permission_filter(Action.CREATE)
        if isinstance(filt, NoMatchFilter):
            raise ForbiddenError(self._resource_name, Action.CREATE.value)
        return await self._inner.create(payload)

    async def read(self, id: K) -> T:  # noqa: A002
        result = await self._inner.read(id)
        filt = await self._permission_filter(Action.READ)
        if result is None or not filt.matches(result):
            raise NotFoundError(id)
        return result

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        """Apply an in-scope update, folding the policy filter into the write.

        The ``UPDATE`` policy filter is combined with the caller's ``condition``
        and pushed into the backend's write, so authorization is decided
        atomically with the write (no read-then-write race). A miss — absent,
        out of scope, or a failed caller condition — is a 404, so a denied row
        is indistinguishable from an absent one.
        """
        filt = await self._permission_filter(Action.UPDATE)
        result = await self._inner.update(payload, condition=_combine(filt, condition))
        if result is None:
            raise NotFoundError(self._payload_id(payload))
        return result

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        """Delete an in-scope row, folding the policy filter into the write.

        As :meth:`update`: the ``DELETE`` policy filter rides along as the
        write's condition, so a denied row (and an absent one) is a 404.
        """
        filt = await self._permission_filter(Action.DELETE)
        deleted = await self._inner.delete(id, condition=_combine(filt, condition))
        if not deleted:
            raise NotFoundError(id)
        return deleted

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        perm = await self._permission_filter(Action.SEARCH)
        combined = and_(perm, search_filter) if search_filter is not None else perm
        return await self._inner.search(combined, sort_order, cursor, limit)

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        perm = await self._permission_filter(Action.COUNT)
        combined = and_(perm, search_filter) if search_filter is not None else perm
        return await self._inner.count(combined)

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        results = await self._inner.batch_read(ids)
        filt = await self._permission_filter(Action.BATCH_READ)
        return [item if item is not None and filt.matches(item) else None for item in results]

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Apply only in-scope edits; other positions are ``None``.

        A create is applied when creating is permitted. An update / delete has
        the ``BATCH_EDIT`` policy filter folded into its own condition, so the
        backend enforces scope **atomically with the write** (no read-then-write
        race): an out-of-scope / absent / condition-failed edit writes nothing
        and yields ``None``. The denied positions are ``None`` (no write),
        positionally aligned with ``edits``.
        """
        filt = await self._permission_filter(Action.BATCH_EDIT)
        flags: list[bool] = []
        permitted: list[Create[T] | Update[T] | Delete[K]] = []
        for edit in edits:
            if isinstance(edit, Create):
                allowed = not isinstance(filt, NoMatchFilter)
                flags.append(allowed)
                if allowed:
                    permitted.append(edit)
                continue
            # An update / delete with no identifier cannot be scoped, so it is a
            # miss (None) rather than being handed to the backend to raise.
            target = edit.id if isinstance(edit, Delete) else self._payload_id(edit.item)
            if target is MISSING or target is None:
                flags.append(False)
                continue
            flags.append(True)
            if isinstance(edit, Update):
                permitted.append(Update(item=edit.item, condition=_combine(filt, edit.condition)))
            else:
                permitted.append(Delete(id=edit.id, condition=_combine(filt, edit.condition)))
        if not permitted:
            return [None] * len(edits)
        edited = iter(await self._inner.batch_edit(permitted))
        return [next(edited) if allowed else None for allowed in flags]

    def _payload_id(self, payload: T) -> Any:
        """The identifier carried on an update payload (``MISSING`` if absent)."""
        return getattr(payload, self._id_field, MISSING)


def _combine(
    policy_filter: SearchFilter[Any], condition: SearchFilter[Any] | None
) -> SearchFilter[Any]:
    """Fold the caller's ``condition`` under ``policy_filter`` (AND).

    Both must hold for the write to apply. ``and_`` normalisation means an
    ``AllFilter`` policy (``AllowAll``) drops out and a ``NoMatchFilter`` policy
    (``DenyAll``, or an un-roled / anonymous fail-closed default) annihilates
    the condition — so a denied write misses (``None`` / ``False``) rather than
    raising a distinguishable error, and existence never leaks.
    """
    if condition is None:
        return policy_filter
    return and_(policy_filter, condition)
