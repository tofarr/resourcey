"""Simple, per-app roles and the centralized ``role -> policy`` translation (issue #132).

Part 1 (#131) made ``Principal -> Policy`` a pluggable
:class:`~resourcey.v2.auth.auth_policy.PolicyResolver`. This module is the
**simple-roles** rung: a small, app-declared role vocabulary, transported on the
credential so a role authenticates without a store lookup, and translated to
policies in **one place**.

Two deliberate choices:

* **Roles are per-app, never global.** The same string (``"ADMIN"``) can mean
  different things in different apps, so anything that names roles is scoped to
  the app — declared as a :class:`AppRole` subclass, mapped by an app's
  :class:`RolePolicyResolver`. There is deliberately no process-wide registry
  unioning every app's roles.
* **The transported form is a plain string.** :class:`AppRole` is the ergonomic
  vocabulary (``AppRole.ADMIN``, no magic strings), but a credential carries the
  string (``"ADMIN"``), so the framework never imports app code to interpret a
  credential. :func:`role_key` / :func:`roles_from_credential` are the small
  conversion seam.

:class:`RolePolicyResolver` is the single location where an app expresses its
``role -> policy`` rules. It supports a **global** mapping (a role's policies on
every resource) and a **per-resource** mapping (keyed by the resource's path), an
explicit **fail-closed default** for un-roled / anonymous callers, and the union
model: a principal with several roles gets the OR-combination of their policies,
so a ``DenyAll`` from one role does not override another's grant (see
``specs/permissions.qnt``).

This rung is **static** — the mapping is declared in app code / config. The
store-backed resolver that looks roles up per request is Part 3.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field

from resourcey.v2.auth.auth_policy import Policy, PolicyResolver
from resourcey.v2.auth.auth_principal import Principal
from resourcey.v2.core.resource import Resource


class AppRole(StrEnum):
    """Base class for an app's role vocabulary.

    Subclass it to declare the roles an app recognizes, so routing can name them
    without magic strings::

        class Role(AppRole):
            ADMIN = "ADMIN"
            USER = "USER"

    The members' values *are* the transported role strings; the base itself has
    no members, so the framework never assumes a role set. A role is scoped to
    the app that declares it — two apps may both have an ``ADMIN`` meaning
    different things.
    """


# A role in the app's vocabulary (an ``AppRole`` member) or its raw string form
# as it arrives on a credential. Named for documentation; no runtime wrapping.
RoleName = str


def role_key(role: AppRole | str) -> str:
    """The plain string a role is stored / transported / compared as.

    An :class:`AppRole` member reduces to its value; a string passes through, so
    a credential-carried role needs no app import.
    """
    return role.value if isinstance(role, AppRole) else str(role)


def role_keys(roles: Iterable[AppRole | str]) -> frozenset[str]:
    """Normalise a collection of roles to their plain-string form."""
    return frozenset(role_key(role) for role in roles)


def roles_from_credential(raw: str | Iterable[str] | None) -> frozenset[str]:
    """Normalise a credential-carried role value to a set of role strings.

    A credential may carry one role (a bare string, or a comma-separated list) or
    several (a JSON / sequence). Blank entries are dropped, so an unset value
    yields the empty set (an un-roled caller).
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        candidates = raw.split(",")
        return frozenset(stripped for item in candidates if (stripped := item.strip()))
    return frozenset(str(item).strip() for item in raw if str(item).strip())


class RolePolicyResolver(PolicyResolver):
    """The centralized ``role -> policy`` translation for an app.

    The resolver is the **one place** an app expresses its authorization rules.
    A principal's roles are looked up in the global :attr:`role_policies` map and
    in the per-resource :attr:`resource_role_policies` map (keyed by the target
    resource's path), and the matching policies are unioned by
    :class:`~resourcey.v2.auth.auth_authorized_service.AuthorizedService`.

    Fail-closed by default: an un-roled or anonymous caller, or a caller whose
    roles match nothing, resolves to :attr:`default` (empty unless the app opts
    in). An unknown role therefore grants nothing rather than everything.

    Attributes:
        role_policies: Global rules — role name -> policies on *every* resource.
        resource_role_policies: Per-resource rules — resource path -> role name
            -> policies. A role's global and per-resource policies are both
            *added* to the union (they are not narrowed by each other), so a
            broader grant wins: declaring ``ReadOnly`` globally and ``Owner``
            per resource yields read-all (``ReadOnly``'s ``AllFilter`` dominates
            the union), not own-rows-only. To scope a role per resource, declare
            the policy only in ``resource_role_policies``.
        default: Policies for a caller whose roles matched nothing (or who has no
            roles). Empty by default — fail-closed.
        resource_defaults: Per-resource overrides for :attr:`default`, keyed by
            resource path.
    """

    role_policies: dict[str, list[Policy]] = Field(default_factory=dict)
    resource_role_policies: dict[str, dict[str, list[Policy]]] = Field(default_factory=dict)
    default: list[Policy] = Field(default_factory=list)
    resource_defaults: dict[str, list[Policy]] = Field(default_factory=dict)

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies ``principal``'s roles grant on ``resource``.

        The per-resource map is keyed by the resource's path, so the same role
        can be scoped differently per resource. When no role matches (an
        un-roled / anonymous caller, or an unknown role) the resolver falls back
        to its (fail-closed) default for that resource.
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
