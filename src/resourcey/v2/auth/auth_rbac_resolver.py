"""The store-backed ``PolicyResolver`` for ``v2`` RBAC (issue #133).

:class:`RbacPolicyResolver` implements Part 1's
:class:`~resourcey.v2.auth.auth_policy.PolicyResolver` seam against the stored
RBAC tables: for each request it resolves the authenticated principal's
**groups → roles → permissions** for the target resource and OR-combines them
(the union model — a ``DenyAll`` contributes nothing; an empty set is
fail-closed). It is Part 3's answer to #131's forward-compatible seam.

Resolution
----------
1. Take the authenticated :class:`~resourcey.v2.auth.auth_principal.Principal`
   (whose credential carries a ``user_id`` / ``sub``). An anonymous principal
   (``id is None``) resolves to the empty set (deny / fail-closed).
2. Ask the :class:`~resourcey.v2.auth.auth_rbac_store.RbacStore` for the
   principal's groups and the role → permission policies **scoped to the target
   resource** (never the principal's permissions for other resources).
3. Bind the principal's group membership onto any
   :class:`~resourcey.v2.auth.auth_policy.GroupMember` policy, so its pure
   reduction can branch on it.
4. Return the policies; ``AuthorizedService`` OR-combines their reductions.

Completeness + scaling
----------------------
The result is **complete** for ``(principal, resource)`` — a disjunction cannot
be evaluated from a partial view — but it is **collapsed at the store**: a
scoped query, ``SELECT DISTINCT``, and a single ``IN`` set for same-attribute
grants. The materialized-ACL join/subquery is the documented escape hatch for a
genuinely unbounded per-object grant set (see
:meth:`materialized_acl` and
:mod:`resourcey.v2.auth.auth_rbac_store`).

Freshness / the validation threshold
------------------------------------
A membership change is honoured no later than the credential's validation
threshold (Part 1's cookie ``exp`` / refresh threshold). That trade-off is
**explicit and configurable** here: :attr:`cache_ttl` bounds how long a resolved
policy set is reused. ``None`` (the default) resolves every request, so an API
key — which has no freshness threshold — sees a membership change immediately;
an app serving cookies can set ``cache_ttl`` to (at most) its refresh window and
accept the same staleness the credential already does.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, PrivateAttr

from resourcey.v2.auth.auth_policy import Acl, Policy, PolicyResolver, bind_member_groups
from resourcey.v2.auth.auth_principal import Principal
from resourcey.v2.auth.auth_rbac_store import RbacStore
from resourcey.v2.core.resource import Resource


class RbacPolicyResolver(PolicyResolver):
    """Resolve a principal's stored groups → roles → permissions, per request.

    Attributes:
        store: The read seam over the RBAC tables (e.g.
            :class:`~resourcey.v2.auth.auth_rbac_store.SqlRbacStore`).
        cache_ttl: How long a resolved policy set may be reused, or ``None``
            (the default) to resolve on every request. It is the explicit
            freshness knob: a membership change is honoured no later than
            ``cache_ttl`` (and no later than the credential's own validation
            threshold). Keep it at or below the cookie refresh window.
        clock: The monotonic clock the cache reads, injectable for tests.
    """

    store: RbacStore
    cache_ttl: timedelta | None = None
    clock: Callable[[], float] = time.monotonic

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Resolution cache: ``(principal_id, resource_path) -> (policies, expiry)``.
    # A private attribute (never a field) so it stays out of serialization and
    # is not part of the resolver's declared configuration.
    _cache: dict[tuple[UUID, str], tuple[list[Policy], float]] = PrivateAttr(default_factory=dict)

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies ``principal``'s stored roles grant on ``resource``.

        An anonymous principal resolves to the empty set (deny / fail-closed).
        A store miss likewise yields the empty set, never an implicit grant.
        """
        if principal is None or principal.id is None:
            return []
        path = resource.get_resource_path()
        key = (principal.id, path)
        cached = self._cached(key)
        if cached is not None:
            return cached
        groups = await self.store.groups_for(principal.id)
        policies = await self.store.policies_for(principal.id, path)
        bound = [bind_member_groups(policy, groups) for policy in policies]
        self._store_cached(key, bound)
        return bound

    async def materialized_acl(self, principal: Principal | None, resource: str) -> Acl | None:
        """The materialized per-object ACL for ``principal`` on ``resource``.

        The documented escape hatch: a genuinely unbounded set of per-object
        grants lives in ``resource_acls`` and is evaluated as a join / subquery
        (``RbacStore.acl_id_subquery``), not enumerated into Python. This helper
        is the portable bridge — it enumerates the (capped) ids into an
        :class:`~resourcey.v2.auth.auth_policy.Acl` policy for a small set; beyond
        the cap the store raises and the caller must use the join flavour
        directly. Returns ``None`` when the principal is anonymous or has no ACL
        entries.
        """
        if principal is None or principal.id is None:
            return None
        ids = await self.store.acl_ids(principal.id, resource)
        return Acl(ids=frozenset(ids)) if ids else None

    def invalidate(self, principal_id: UUID | None = None, resource: str | None = None) -> None:
        """Drop cached resolutions, optionally narrowed to a principal / resource.

        For an app that writes the RBAC tables and wants the change to take
        effect before :attr:`cache_ttl` lapses. With no arguments the whole
        cache is cleared.

        The cache is **process-local**: with multiple workers / pods this clears
        only the calling worker, so :attr:`cache_ttl` (not this call) is the
        bound a cross-worker deployment can rely on. With the default
        ``cache_ttl=None`` there is no cache to clear.
        """
        if principal_id is None and resource is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            pid, path = key
            if (principal_id is None or pid == principal_id) and (
                resource is None or path == resource
            ):
                del self._cache[key]

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cached(self, key: tuple[UUID, str]) -> list[Policy] | None:
        if self.cache_ttl is None:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        policies, expiry = entry
        if self.clock() >= expiry:
            del self._cache[key]
            return None
        return list(policies)

    def _store_cached(self, key: tuple[UUID, str], policies: list[Policy]) -> None:
        if self.cache_ttl is None:
            return
        self._cache[key] = (list(policies), self.clock() + self.cache_ttl.total_seconds())
