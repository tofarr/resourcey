"""The store-backed ``PolicyResolver`` for RBAC (issue #133).

:class:`RbacPolicyResolver` implements the ``PolicyResolver`` seam against the
stored RBAC tables: for each request it resolves the authenticated principal's
**groups → roles → permissions** for the target resource and OR-combines them
(the union model — a ``DenyAll`` contributes nothing; an empty set is
fail-closed).

Resolution: take the principal (an anonymous one resolves to the empty set), ask
the store for the principal's groups and the resource-scoped policies, bind the
group membership onto any ``GroupMember`` policy, and return the policies.

The result is **complete** for ``(principal, resource)`` but **collapsed at the
store**: a scoped query, ``SELECT DISTINCT``, and a single ``IN`` set for
same-attribute grants. The materialized-ACL join/subquery is the documented
escape hatch for a genuinely unbounded per-object grant set.

Freshness is explicit: :attr:`cache_ttl` bounds how long a resolved policy set is
reused. ``None`` (the default) resolves every request, so an API key — which has
no freshness threshold — sees a membership change immediately.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, PrivateAttr

from resourcey.auth.auth_policy import Acl, Policy, PolicyResolver, bind_member_groups
from resourcey.auth.auth_principal import Principal
from resourcey.auth.auth_rbac_store import RbacStore
from resourcey.core.resource import Resource


class RbacPolicyResolver(PolicyResolver):
    """Resolve a principal's stored groups → roles → permissions, per request.

    ``cache_ttl`` is the explicit freshness knob: a membership change is
    honoured no later than ``cache_ttl`` (and no later than the credential's
    own validation threshold). Keep it at or below the cookie refresh window.
    """

    store: RbacStore
    cache_ttl: timedelta | None = None
    clock: Callable[[], float] = time.monotonic

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # A private attribute (never a field) so it stays out of serialization.
    _cache: dict[tuple[UUID, str], tuple[list[Policy], float]] = PrivateAttr(default_factory=dict)

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies ``principal``'s stored roles grant on ``resource``.

        An anonymous principal or a store miss yields the empty set, never an
        implicit grant.
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

        The portable bridge: it enumerates the (capped) ids into an ``Acl`` policy
        for a small set; beyond the cap the store raises and the caller must use
        the join flavour directly. Returns ``None`` when the principal is anonymous
        or has no ACL entries.
        """
        if principal is None or principal.id is None:
            return None
        ids = await self.store.acl_ids(principal.id, resource)
        return Acl(ids=frozenset(ids)) if ids else None

    def invalidate(self, principal_id: UUID | None = None, resource: str | None = None) -> None:
        """Drop cached resolutions, optionally narrowed to a principal / resource.

        For an app that writes the RBAC tables and wants the change to take effect
        before :attr:`cache_ttl` lapses. The cache is **process-local**: with
        multiple workers this clears only the calling one.
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
