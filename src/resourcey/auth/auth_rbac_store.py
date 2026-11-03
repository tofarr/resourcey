"""The RBAC store seam and its SQL implementation (issue #133).

:class:`RbacStore` is the **read** side of the stored RBAC model — the two
questions a per-request resolution asks of storage: which policies do this
principal's roles grant on this resource, and which groups is it a member of. The
store also exposes the **materialized-ACL** escape hatch
(:meth:`RbacStore.acl_ids` / :meth:`RbacStore.acl_id_subquery`).

Scaling the resolution
----------------------
The resolution must stay **complete** for ``(principal, resource, action)`` — a
disjunction cannot be evaluated from a partial view — but it must not expand
``K`` grants into a ``K``-term ``OR``. :class:`SqlRbacStore` therefore
**collapses at the store**: the query is scoped to the target resource and the
principal's groups, a ``SELECT DISTINCT`` collapses roles that share a policy, and
many grants still collapse under the union in ``AuthorizedService``.

The materialized ACL
--------------------
When per-object grants are genuinely unbounded, enumerate them in
``resource_acls`` and evaluate the permission as a join / subquery so the grants
never enter Python. This table is derived data and is invalidation-sensitive;
prefer a broad role rule. The enumeration path is capped
(:data:`ACL_MAX_IDS`), beyond which the join flavour is the answer.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import inspect
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.auth.auth_policy import Policy
from resourcey.auth.auth_rbac import GroupRole, GroupUser, ResourceAcl, RolePermission
from resourcey.core.errors import ResourceyConfigError
from resourcey.util.models import get_known_concrete_subclasses

logger = logging.getLogger(__name__)

#: Failure signatures already logged, so a per-request resolution warns once per
#: distinct problem rather than once per request.
_warned: set[tuple[str, ...]] = set()
_warned_lock = threading.Lock()

#: The most object ids an enumerated ACL may carry. Beyond this the
#: materialized-ACL join / subquery is the answer, not a literal ``IN`` list.
ACL_MAX_IDS = 100


class RbacStore(ABC):
    """The read seam over the stored RBAC tables.

    A resolver depends on this, not on a session, so the resolution is testable
    without a live database and a non-SQL store stays expressible.
    """

    @abstractmethod
    async def policies_for(self, user_id: UUID, resource: str) -> list[Policy]:
        """The policies ``user_id``'s roles grant on ``resource`` (complete)."""
        raise NotImplementedError

    @abstractmethod
    async def groups_for(self, user_id: UUID) -> frozenset[UUID]:
        """The ids of the groups ``user_id`` is a member of."""
        raise NotImplementedError

    @abstractmethod
    async def acl_ids(self, user_id: UUID, resource: str) -> list[str]:
        """The enumerated per-object grants for ``(user_id, resource)`` (capped)."""
        raise NotImplementedError

    @abstractmethod
    def acl_id_subquery(self, user_id: UUID, resource: str) -> Select[tuple[str]]:
        """The materialized-ACL join / subquery (``resource_id`` for the pair)."""
        raise NotImplementedError


class SqlRbacStore(RbacStore):
    """The SQL store over the RBAC tables, backed by a session source.

    Args:
        session_factory: Either an ``async_sessionmaker`` the store opens
            short-lived sessions from, or a zero-arg callable returning one
            (e.g. a closure over an entered
            :class:`~resourcey.sql.session_manager.SqlSessionManager`). The
            callable form lets the store resolve the maker lazily, since a
            manager only hands out makers after it is entered. Each call opens,
            uses, and closes its own session, so the store never adopts (or is
            adopted by) a resource's request session — the resolution is
            independent of the target storage.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | Any) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """A short-lived session, opening and closing one per call.

        The session source may be an ``async_sessionmaker`` or a callable
        returning one (sync or async), so a store can be built before the app
        lifecycle is entered.
        """
        source = self._session_factory
        maker = source if isinstance(source, async_sessionmaker) else source()
        if inspect.isawaitable(maker):
            maker = await maker
        async with maker() as session:
            yield session

    async def policies_for(self, user_id: UUID, resource: str) -> list[Policy]:
        """Resolve ``user_id``'s role -> permission policies for ``resource``.

        A single scoped query joins membership → role assignment → permissions.
        ``SELECT DISTINCT`` collapses roles that share a policy. An absent grant
        yields an empty list (fail-closed).
        """
        stmt = (
            select(RolePermission.permission)
            .join(GroupRole, GroupRole.role_id == RolePermission.role_id)
            .join(GroupUser, GroupUser.group_id == GroupRole.group_id)
            .where(GroupUser.user_id == user_id)
            .where(RolePermission.resource == resource)
            .distinct()
        )
        async with self._session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return policy_from_rows(rows, resource=resource)

    async def groups_for(self, user_id: UUID) -> frozenset[UUID]:
        """The ids of the groups ``user_id`` belongs to."""
        stmt = select(GroupUser.group_id).where(GroupUser.user_id == user_id).distinct()
        async with self._session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return frozenset(rows)

    async def acl_ids(self, user_id: UUID, resource: str) -> list[str]:
        """The enumerated per-object grants for ``(user_id, resource)``.

        Raises when the set exceeds :data:`ACL_MAX_IDS`, pointing at the join
        flavour.
        """
        stmt = self.acl_id_subquery(user_id, resource)
        async with self._session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) > ACL_MAX_IDS:
            raise ResourceyConfigError(
                f"Materialized ACL for principal {user_id} on {resource!r} has {len(rows)} "
                f"entries, over the {ACL_MAX_IDS}-entry enumeration cap. Use the join / "
                "subquery flavour (RbacStore.acl_id_subquery) instead of a literal IN list."
            )
        return rows

    def acl_id_subquery(self, user_id: UUID, resource: str) -> Select[tuple[str]]:
        """The ``resource_acls`` subquery keyed by ``(principal_id, resource_name)``.

        Used directly inside a WHERE clause (``WHERE id IN <subquery>``) so a large
        ACL never materializes in Python.
        """
        return select(ResourceAcl.resource_id).where(
            ResourceAcl.principal_id == user_id,
            ResourceAcl.resource_name == resource,
        )


def policy_from_rows(rows: Sequence[Any], *, resource: str | None = None) -> list[Policy]:
    """Deserialize stored permission rows into policy objects (best-effort).

    A row that cannot be deserialized is **skipped** rather than crashing the
    request or denying everything: one bad row must not take down the whole
    access decision. The skip fails **closed** — a dropped row never becomes a
    grant.

    Skipping is not silent, because the two causes need different fixes and one
    of them points nowhere near the symptom. A row whose ``kind`` names no loaded
    policy class means the app forgot to import the module defining it; that is
    logged once per kind per process, naming the kind and the resource, so a busy
    endpoint cannot flood the log. A recognizably malformed row is logged once
    per failure signature. Anything that is not a row problem — an error raised
    by a policy's own code, say — propagates instead of being swallowed here.

    Args:
        rows: Stored ``role_permissions.permission`` values.
        resource: The resource the rows were scoped to, for the warnings.
    """
    known_kinds = {loaded.__name__ for loaded in get_known_concrete_subclasses(Policy)}
    policies: list[Policy] = []
    for raw in rows:
        if not isinstance(raw, dict):
            _warn_once(
                ("row", type(raw).__name__),
                "Skipping stored policy on resource %r: expected a mapping, got %s",
                resource,
                type(raw).__name__,
            )
            continue
        kind = raw.get("kind")
        if isinstance(kind, str) and kind not in known_kinds:
            _warn_once(
                ("kind", kind),
                "Skipping stored policies of unknown kind %r on resource %r: no policy "
                "class with that name is loaded, so the grants it describes are being "
                "dropped. Import the module that defines it.",
                kind,
                resource,
            )
            continue
        try:
            policies.append(Policy.model_validate(raw))
        except ValidationError as error:
            _warn_once(
                ("malformed", str(kind), *_error_signature(error)),
                "Skipping malformed stored policy of kind %r on resource %r: %s",
                kind,
                resource,
                error,
            )
    return policies


def _error_signature(error: ValidationError) -> tuple[str, ...]:
    """A stable identity for a validation failure, so repeats collapse in the log."""
    return tuple(f"{detail.get('loc')}:{detail.get('type')}" for detail in error.errors())


def _warn_once(key: tuple[str, ...], message: str, *args: Any) -> None:
    """Log a warning the first time ``key`` is seen in this process.

    Keyed (never resource-scoped) so the seen-set stays bounded by the number of
    distinct failure signatures rather than growing with traffic.
    """
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message, *args)
