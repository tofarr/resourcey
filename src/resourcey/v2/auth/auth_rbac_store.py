"""The RBAC store seam and its SQL implementation (issue #133).

:class:`RbacStore` is the **read** side of the stored RBAC model — the two
questions a per-request resolution asks of storage:

* *what policies do this principal's roles grant on this resource?*
  (:meth:`RbacStore.policies_for`), and
* *what groups is this principal a member of?* (:meth:`RbacStore.groups_for`),
  so a :class:`~resourcey.v2.auth.auth_policy.GroupMember` can branch on
  membership.

The store also exposes the **materialized-ACL** escape hatch
(:meth:`RbacStore.acl_ids` / :meth:`RbacStore.acl_id_subquery`), which is the
documented high-end path for a genuinely unbounded set of per-object grants.

Scaling the resolution
----------------------
A principal may hold a large number of grants. The resolution must stay
**complete** for ``(principal, resource, action)`` — a disjunction cannot be
evaluated from a partial view — but it must not expand ``K`` grants into a
``K``-term ``OR``. :class:`SqlRbacStore` therefore **collapses at the store**:

* the query is scoped to the target resource and the principal's groups, so a
  principal's permissions for *other* resources are never loaded;
* a ``SELECT DISTINCT`` collapses roles that share a policy;
* many grants still collapse under the union in
  :class:`~resourcey.v2.auth.auth_authorized_service.AuthorizedService`
  (``k`` read grants reduce to ``AllFilter``; same-attribute ACLs reduce to one
  ``IN`` set via :class:`~resourcey.v2.util.search_filter.InFilter`).

The materialized ACL
--------------------
When per-object grants are genuinely unbounded, enumerate them in
``resource_acls`` (keyed by ``(principal_id, resource_name)``) and evaluate the
permission as a **join / subquery** so the grants never enter Python::

    WHERE id IN (
        SELECT resource_id FROM resource_acls
        WHERE principal_id = :principal AND resource_name = :resource
    )

:meth:`SqlRbacStore.acl_id_subquery` returns exactly that subquery. This table
is derived data: it is a second cache to maintain on *every* write and it
inherits the credential-threshold staleness (worse, because it is
invalidation-sensitive). Prefer a broad role rule — "everything I own" — to
enumerating object ids. The enumeration path is capped
(:data:`ACL_MAX_IDS`): a model that reaches for it is forced to say so
explicitly, and beyond the cap the join flavour is the answer, not a literal
``IN`` list.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.v2.auth.auth_policy import Policy
from resourcey.v2.auth.auth_rbac import GroupRole, GroupUser, ResourceAcl, RolePermission
from resourcey.v2.core.errors import ResourceyConfigError

#: The most object ids an enumerated ACL may carry (v1's ``ACL_MAX_IDS`` carried
#: forward). Beyond this the materialized-ACL join / subquery is the answer, not
#: a literal ``IN`` list.
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
            :class:`~resourcey.v2.sql.session_manager.SqlSessionManager`). The
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
        returning one (sync or async), so a store can be built before an app
        lifecycle (a ``SqlSessionManager`` only hands out makers once entered).
        """
        source = self._session_factory
        maker = source if isinstance(source, async_sessionmaker) else source()
        if inspect.isawaitable(maker):
            maker = await maker
        async with maker() as session:
            yield session

    async def policies_for(self, user_id: UUID, resource: str) -> list[Policy]:
        """Resolve ``user_id``'s role -> permission policies for ``resource``.

        A single scoped query joins membership → role assignment → permissions,
        filtered to the target ``resource``. ``SELECT DISTINCT`` collapses roles
        that share a policy (a common case: two roles both granting ``AllowAll``)
        so the union never sees a duplicate. An absent grant yields an empty list
        (fail-closed).
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
        return policy_from_rows(rows)

    async def groups_for(self, user_id: UUID) -> frozenset[UUID]:
        """The ids of the groups ``user_id`` belongs to."""
        stmt = select(GroupUser.group_id).where(GroupUser.user_id == user_id).distinct()
        async with self._session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return frozenset(rows)

    async def acl_ids(self, user_id: UUID, resource: str) -> list[str]:
        """The enumerated per-object grants for ``(user_id, resource)``.

        Raises :class:`~resourcey.v2.core.errors.ResourceyConfigError` when the
        set exceeds :data:`ACL_MAX_IDS`: a model that reaches for the enumeration
        path is told so explicitly, and pointed at the join flavour.
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

        This is the join flavour, used directly inside a WHERE clause
        (``WHERE id IN <subquery>``) so a large ACL never materializes in Python.
        """
        return select(ResourceAcl.resource_id).where(
            ResourceAcl.principal_id == user_id,
            ResourceAcl.resource_name == resource,
        )


def policy_from_rows(rows: Sequence[Any]) -> list[Policy]:
    """Deserialize stored permission rows into policy objects (best-effort).

    A corrupt / unrecognized policy row is **skipped** rather than crashing the
    request or denying everything: one bad row must not take down the whole
    access decision. A row that is not a mapping is likewise skipped.
    """
    policies: list[Policy] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            policies.append(Policy.model_validate(raw))
        except Exception:
            continue
    return policies
