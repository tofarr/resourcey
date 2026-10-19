"""Authorization policies (issue #127).

A :class:`Policy` is a discriminated-union object that knows how to *reduce
itself* to a :class:`~resourcey.util.search_filter.SearchFilter` for an
:class:`~resourcey.core.service.Action`. Storing the rule (rather than a
serialized filter) keeps the decision logic co-located with the data and lets one
stored rule adapt as the requested action changes.

The name is ``Policy``, not ``Permission``: a permission is the *computed right*
a principal has, while the policy is the *rule* that computes it.

The principal is an **argument** to the reduction, not state on the policy, so a
policy stays a storable, serializable value — the property a user / group / role
store needs.

Built-in policies:

* :class:`AllowAll` — grants every action over every row.
* :class:`DenyAll`  — denies every action over every row.
* :class:`ReadOnly` — grants read / search / count / batch_read, denies writes.
* :class:`Owner`    — scopes rows to the authenticated principal.
* :class:`GroupMember` — a per-action outcome selected by group membership.
* :class:`Acl` — a bounded set of permitted object identifiers.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import Field, PrivateAttr

from resourcey.core.service import Action
from resourcey.util.models import DiscriminatedUnionMixin
from resourcey.util.search_filter import (
    AllFilter,
    AttrFilter,
    EqFilter,
    InFilter,
    NoMatchFilter,
    SearchFilter,
)

# The actions that only read; the write actions are excluded.
_READ_LIKE_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)

if TYPE_CHECKING:
    from resourcey.auth.auth_principal import Principal
    from resourcey.core.resource import Resource


class Policy(DiscriminatedUnionMixin, ABC):
    """Abstract authorization rule: reduce to a search filter for an action.

    A stored policy serializes to JSON and deserializes back to the right
    subclass. :meth:`to_search_filter` answers the one question the enforcement
    layer asks: *which rows may this principal touch, for this action?* An
    ``AllFilter`` means every row; a ``NoMatchFilter`` means none.
    """

    # Whether this policy's reduction depends on *who* is asking. Defaults to
    # ``True`` (safe), so a narrowed response is marked caller-private. Not a
    # pydantic field -- a class-level fact about the policy kind.
    scopes_to_caller: ClassVar[bool] = True

    group_ids: list[uuid.UUID] = Field(default_factory=list)
    """The group ids a membership policy applies to (its *target* groups).

    A plain policy ignores this. A ``list`` (not a ``frozenset``) so a stored
    policy serializes to JSON.
    """

    @abstractmethod
    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        """The rows this policy admits for ``(user_id, action)``.

        Async so a future policy may consult storage; the built-ins are pure.
        """
        raise NotImplementedError


class AllowAll(Policy):
    """Grants every action over every row."""

    scopes_to_caller: ClassVar[bool] = False

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        return AllFilter()


class DenyAll(Policy):
    """Denies every action over every row.

    A ``NoMatchFilter`` contributes nothing to the union of a principal's
    policies: it does not override another policy's grant. On its own it denies
    everything.
    """

    scopes_to_caller: ClassVar[bool] = False

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        return NoMatchFilter()


class ReadOnly(Policy):
    """Grants the read-like actions and denies the write actions."""

    scopes_to_caller: ClassVar[bool] = False

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if action in _READ_LIKE_ACTIONS:
            return AllFilter()
        return NoMatchFilter()


class Owner(Policy):
    """Scopes rows to the authenticated principal — the "own-rows-only" rule.

    Read-like and write-by-id actions reduce to ``AttrFilter(owner_field,
    EqFilter(user_id))``, so a row owned by someone else is a 404 and not
    leaked. ``create`` is unscoped — a new row has no owner yet — so it is
    granted whenever the caller is authenticated. An anonymous caller owns
    nothing and is denied.

    ``owner_field`` defaults to ``user_id``; set it to the resource's actual
    owner column via the role resolver's per-resource policy value.
    """

    owner_field: str = "user_id"

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if user_id is None:
            return NoMatchFilter()
        if action is Action.CREATE:
            # A new row has no owner yet; the resource stamps it.
            return AllFilter()
        return AttrFilter(attribute=self.owner_field, filter=EqFilter(value=user_id))


class GroupMember(Policy):
    """Group-membership gate: outcome selected by the principal's group membership.

    The principal-level counterpart of :class:`Owner`. If the caller belongs to
    at least one of :attr:`group_ids`, the reduction is ``on_match``; otherwise
    ``on_mismatch``. ``create`` always uses ``on_create``. All three default to
    :class:`DenyAll`, so a bare ``GroupMember()`` grants nothing.

    Membership is **pre-bound** by the resolver onto the in-memory copy, so the
    reduction stays pure and storage-free. An unbound policy reduces to
    ``on_mismatch`` — fail-closed.
    """

    on_match: Policy = Field(default_factory=DenyAll)
    on_mismatch: Policy = Field(default_factory=DenyAll)
    on_create: Policy = Field(default_factory=DenyAll)

    _member_groups: frozenset[uuid.UUID] = PrivateAttr(default_factory=frozenset)

    @property
    def member_groups(self) -> frozenset[uuid.UUID]:
        """The principal's bound groups (never a stored field).

        Kept off the JSON surface: the resolved membership is process-local and
        never persisted, so it must not appear in ``model_dump``.
        """
        return self._member_groups

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return await self.on_create.to_search_filter(user_id, action)
        # An unbound copy (empty membership) reduces to ``on_mismatch``.
        if not self._member_groups.isdisjoint(self.group_ids):
            return await self.on_match.to_search_filter(user_id, action)
        return await self.on_mismatch.to_search_filter(user_id, action)


def bind_member_groups(policy: Policy, groups: frozenset[uuid.UUID]) -> Policy:
    """Bind a principal's group membership onto a membership-aware policy.

    Binds onto the in-memory copy (never the stored value), so the pure
    reduction can answer "is the caller a member?". A non-membership policy is
    returned unchanged.
    """
    if isinstance(policy, GroupMember):
        bound = policy.model_copy()
        bound._member_groups = groups
        return bound
    return policy


class Acl(Policy):
    """Portable object-id-list policy — the materialized-ACL escape hatch.

    A bounded set of identifiers the principal may touch, reduced to a single
    ``IN`` predicate for the non-create actions. Beyond the set cap a genuine
    per-object ACL must be pushed down as a store join / subquery instead.

    ``create`` is unscoped (a new row has no id yet); use ``on_create`` to deny
    it if desired.
    """

    ids: frozenset[Any] = frozenset()
    id_field: str = "id"
    on_create: Policy = Field(default_factory=AllowAll)

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return await self.on_create.to_search_filter(user_id, action)
        if not self.ids:
            return NoMatchFilter()
        return AttrFilter(attribute=self.id_field, filter=InFilter(values=tuple(self.ids)))


class PolicyResolver(DiscriminatedUnionMixin, ABC):
    """Reduce an authenticated principal to the policies for one resource.

    A resolver maps a principal (its id, roles, ...) to the policies that apply
    to the target resource, and ``AuthorizedService`` OR-combines their
    reductions (a ``DenyAll`` contributes nothing; an empty list is
    fail-closed).

    It is **async** and receives the target ``resource`` so a resolver may
    consult storage and scope per resource. The result must be *complete* for
    ``(principal, resource)``, so a resolver scopes and collapses at the store
    rather than expanding many grants into a tree.
    """

    @abstractmethod
    async def resolve(
        self, resource: Resource[Any, Any], principal: Principal | None
    ) -> list[Policy]:
        """The policies that apply to ``principal`` on ``resource``."""
        raise NotImplementedError


class AllowAllResolver(PolicyResolver):
    """Grants every caller full access (``AllowAll``) — the default.

    An anonymous caller still gets ``AllowAll`` here; whether anonymous access is
    *reached* is the dependency's decision (strict vs lenient), not the
    resolver's.
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
