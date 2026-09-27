"""Authorization policies for ``v2`` (issue #127).

A :class:`Policy` is a discriminated-union (``DiscriminatedUnionMixin``) object
that knows how to *reduce itself* to a
:class:`~resourcey.v2.util.search_filter.SearchFilter` for an
:class:`~resourcey.v2.core.service.Action`. Storing the rule (rather than a
serialized filter) keeps the decision logic co-located with the data and lets
one stored rule adapt as the requested action changes — the model ported from
v1's ``resourcey.auth.permission`` (issue #4), minus the user / group / role
machinery.

The name is ``Policy``, not ``Permission``: a permission is the *computed
right* a principal has, while the policy is the *rule* that computes it. The
project is pre-release, so the clearer name is preferred over the v1 one; the
modelled rule in ``specs/permissions.qnt`` is likewise called a ``Policy``.

The principal is an **argument** to the reduction, not state on the policy, so a
policy stays a storable, serializable value — the property a later user / group
/ role store needs. ``user_id`` is ``None`` throughout this first rung (there is
no authenticated principal yet); it is the forward-compatible seam for the
users / groups / roles work.

Built-in policies:

* :class:`AllowAll` — grants every action over every row (``AllFilter``).
* :class:`DenyAll`  — denies every action over every row (``NoMatchFilter``).
* :class:`ReadOnly` — grants read / search / count / batch_read, denies the
  write actions.
* :class:`Owner`    — scopes rows to the authenticated principal (the
  "own-rows-only" rule): reads / searches / updates / deletes only rows whose
  owner column equals ``user_id``.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from resourcey.v2.core.service import Action
from resourcey.v2.util.models import DiscriminatedUnionMixin
from resourcey.v2.util.search_filter import (
    AllFilter,
    AttrFilter,
    EqFilter,
    NoMatchFilter,
    SearchFilter,
)

# The read-like actions a ReadOnly policy grants: the four actions that only
# read. The write actions (create / update / delete / batch_edit) are excluded.
_READ_LIKE_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)

if TYPE_CHECKING:
    from resourcey.v2.auth.auth_principal import Principal
    from resourcey.v2.core.resource import Resource


class Policy(DiscriminatedUnionMixin, ABC):
    """Abstract authorization rule: reduce to a search filter for an action.

    Concrete subclasses participate in the discriminated-union machinery (a
    ``kind`` computed field tags the concrete type), so a stored policy can be
    serialized to JSON and deserialized back to the right subclass. A subclass
    implements :meth:`to_search_filter`, which answers the one question the
    enforcement layer asks: *which rows may this principal touch, for this
    action?*

    A returned :class:`~resourcey.v2.util.search_filter.AllFilter` means "every
    row"; a :class:`~resourcey.v2.util.search_filter.NoMatchFilter` means "no
    row" (a deny — no writes, an empty read).
    """

    @abstractmethod
    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        """The rows this policy admits for ``(user_id, action)``.

        The reduction is **async** so a future policy may consult storage; the
        built-ins are pure and need no IO. The principal is passed explicitly
        rather than bound into the policy, keeping the policy a storable value.
        """
        raise NotImplementedError


class AllowAll(Policy):
    """Grants every action over every row."""

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        return AllFilter()


class DenyAll(Policy):
    """Denies every action over every row.

    A ``NoMatchFilter`` contributes nothing to the union of a principal's
    policies (see ``specs/permissions.qnt``): it does not override another
    policy's grant. On its own it denies everything.
    """

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        return NoMatchFilter()


class ReadOnly(Policy):
    """Grants the read-like actions and denies the write actions.

    The read-like set (:data:`_READ_LIKE_ACTIONS`) is ``read``, ``search``,
    ``count``, and ``batch_read``; everything else (``create``, ``update``,
    ``delete``, ``batch_edit``) reduces to ``NoMatchFilter``.
    """

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if action in _READ_LIKE_ACTIONS:
            return AllFilter()
        return NoMatchFilter()


class Owner(Policy):
    """Scopes rows to the authenticated principal — the "own-rows-only" rule.

    For the read-like actions the reduction is ``AttrFilter(<owner_field>,
    EqFilter(user_id))``: only rows whose owner column equals the principal's id
    are admitted. The write-by-id actions (``update`` / ``delete``) use the same
    scope, so a row owned by someone else is a 404 and not leaked. ``create`` is
    *not* scoped by a filter — a new row has no owner yet — so it is granted
    whenever the caller is authenticated; the row's owner is supplied by the
    resource / service, not by the policy.

    An anonymous caller (``user_id is None``) owns nothing, so every action
    reduces to ``NoMatchFilter`` (deny / fail-closed).

    Attributes:
        owner_field: The DTO field naming the row's owner. Defaults to
            ``user_id``; set it to the resource's actual owner column (e.g.
            ``owner_id``) via the role resolver's per-resource policy value.
    """

    owner_field: str = "user_id"

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if user_id is None:
            return NoMatchFilter()
        if action is Action.CREATE:
            # Create is unscoped: the row does not exist yet, and its owner is
            # stamped by the resource rather than matched by the policy.
            return AllFilter()
        return AttrFilter(attribute=self.owner_field, filter=EqFilter(value=user_id))


class PolicyResolver(DiscriminatedUnionMixin, ABC):
    """Reduce an authenticated principal to the policies for one resource.

    The ``Principal -> Policy`` translation is a **first-class, centralized,
    pluggable** seam: a resolver maps a principal (its id, roles, ...) to the
    policies that apply to the target resource, and
    :class:`~resourcey.v2.auth.auth_authorized_service.AuthorizedService`
    OR-combines their reductions (the union model -- a ``DenyAll`` contributes
    nothing; an empty list is deny / fail-closed).

    It is **async** and receives the target ``resource`` so a resolver may
    consult storage (the store-backed RBAC rung) and scope per resource. The
    result must be *complete* for ``(principal, resource)`` -- a disjunction
    cannot be evaluated from a partial view -- so a resolver scopes and collapses
    at the store rather than expanding many grants into a tree.

    The built-ins below are principal-independent; an app supplies its own
    subclass (or, later, a config-declared mapping) for its role logic.
    """

    @abstractmethod
    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies that apply to ``principal`` on ``resource``."""
        raise NotImplementedError


class AllowAllResolver(PolicyResolver):
    """Grants every authenticated caller full access (``AllowAll``).

    The default, preserving the #118 / #127 posture: any valid credential maps
    to ``AllowAll``. An anonymous caller (``principal is None`` or
    ``principal.id is None``) still gets ``AllowAll`` here -- whether anonymous
    access is *reached* is the dependency's decision (strict vs lenient), not
    the resolver's.
    """

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        return [AllowAll()]


class DenyAllResolver(PolicyResolver):
    """Denies every caller (no policies => fail-closed)."""

    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        return []
