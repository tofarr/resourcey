"""Simple, per-app roles and the centralized ``role -> policy`` translation (issue #132).

Part 1 (#131) made ``Principal -> Policy`` a pluggable ``PolicyResolver``. This
module is the **simple-roles** rung: a small, app-declared role vocabulary,
transported on the credential so a role authenticates without a store lookup, and
translated to policies in **one place**.

Two deliberate choices:

* **Roles are per-app, never global.** The same string (``"ADMIN"``) can mean
  different things in different apps, so anything that names roles is scoped to
  the app. There is deliberately no process-wide role registry.
* **The transported form is a plain string.** :class:`AppRole` is the ergonomic
  vocabulary, but a credential carries the string, so the framework never imports
  app code to interpret a credential. :func:`role_key` /
  :func:`roles_from_credential` are the conversion seam.

:class:`RolePolicyResolver` is the single location where an app expresses its
``role -> policy`` rules, with a global and a per-resource mapping and an
explicit fail-closed default.

This rung is **static** — the mapping is declared in app code / config. The
store-backed resolver that looks roles up per request is Part 3.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field

from resourcey.auth.auth_policy import Policy, PolicyResolver
from resourcey.auth.auth_principal import Principal
from resourcey.core.resource import Resource


class AppRole(StrEnum):
    """Base class for an app's role vocabulary.

    Subclass it to declare the roles an app recognizes::

        class Role(AppRole):
            ADMIN = "ADMIN"
            USER = "USER"

    The members' values *are* the transported role strings; the base itself has
    no members, so the framework never assumes a role set.
    """


# A role as an ``AppRole`` member or its raw credential string.
RoleName = str


def role_key(role: AppRole | str) -> str:
    """The plain string a role is stored / transported / compared as.

    An :class:`AppRole` member reduces to its value; a string passes through.
    """
    return role.value if isinstance(role, AppRole) else str(role)


def role_keys(roles: Iterable[AppRole | str]) -> frozenset[str]:
    """Normalise a collection of roles to their plain-string form."""
    return frozenset(role_key(role) for role in roles)


def roles_from_credential(raw: str | Iterable[str] | None) -> frozenset[str]:
    """Normalise a credential-carried role value to a set of role strings.

    A bare string may be comma-separated. Blank entries are dropped, so an unset
    value yields the empty set (an un-roled caller).
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        candidates = raw.split(",")
        return frozenset(stripped for item in candidates if (stripped := item.strip()))
    return frozenset(str(item).strip() for item in raw if str(item).strip())


class RolePolicyResolver(PolicyResolver):
    """The centralized ``role -> policy`` translation for an app.

    A principal's roles are looked up in the global :attr:`role_policies` map and
    in the per-resource :attr:`resource_role_policies` map, and the matching
    policies are unioned by ``AuthorizedService``. Fail-closed by default: an
    un-roled or anonymous caller resolves to :attr:`default` (empty unless the
    app opts in), so an unknown role grants nothing.

    ``resource_role_policies`` maps a resource path -> role name -> policies. A
    role's global and per-resource policies are both *added* to the union, so
    declaring ``ReadOnly`` globally and ``Owner`` per resource yields read-all,
    not own-rows-only. To scope a role per resource, declare the policy only in
    ``resource_role_policies``.
    """

    role_policies: dict[str, list[Policy]] = Field(default_factory=dict)
    resource_role_policies: dict[str, dict[str, list[Policy]]] = Field(default_factory=dict)
    default: list[Policy] = Field(default_factory=list)
    resource_defaults: dict[str, list[Policy]] = Field(default_factory=dict)

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies ``principal``'s roles grant on ``resource``.

        When no role matches the resolver falls back to its (fail-closed) default
        for that resource.
        """
        path = resource.get_resource_path()
        roles = principal.roles if principal is not None else frozenset()
        per_resource = self.resource_role_policies.get(path, {})
        policies: list[Policy] = []
        for role in roles:
            policies.extend(self.role_policies.get(role, ()))
            policies.extend(per_resource.get(role, ()))
        if not policies:
            return list(self.resource_defaults.get(path, self.default))
        return policies
