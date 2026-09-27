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
* :class:`GroupMember` - the group-membership gate: a per-action outcome
  (``on_match`` / ``on_mismatch`` / ``on_create``) selected by whether the
  principal belongs to one of ``group_ids``. Membership is **pre-bound** by the
  resolver (``matched``), so the reduction stays pure and storage-free.
* :class:`Acl` - the portable object-id-list policy (a materialized ACL): a
  bounded set of identifiers the principal may touch. It is the escape hatch
  for genuine per-object grants; see
  :mod:`resourcey.v2.auth.auth_rbac_resolver` for the store-backed loading and
  its cap.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import Field, PrivateAttr

from resourcey.v2.core.service import Action
from resourcey.v2.util.models import DiscriminatedUnionMixin
from resourcey.v2.util.search_filter import (
    AllFilter,
    AttrFilter,
    EqFilter,
    InFilter,
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

    # Whether this policy's reduction depends on *who* is asking. Defaults to
    # ``True`` (safe): an unclassified policy is assumed caller-scoped, so a
    # response it narrows is marked caller-private and never stored by a shared
    # cache. A genuinely principal-independent policy opts out by declaring
    # ``False``. Not a pydantic field -- a class-level fact about the policy kind.
    scopes_to_caller: ClassVar[bool] = True

    group_ids: list[uuid.UUID] = Field(default_factory=list)
    """The group ids a membership policy applies to (its *target* groups).

    A plain policy ignores this. A store-backed resolver resolves the
    *principal's* groups once per request and binds them onto the policy copy it
    hands to ``AuthorizedService`` (via :func:`bind_member_groups`), so the
    reduction itself stays pure and storage-free. A ``list`` (not a
    ``frozenset``) so a stored policy serializes to JSON.
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

    scopes_to_caller: ClassVar[bool] = False

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

    scopes_to_caller: ClassVar[bool] = False

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

    scopes_to_caller: ClassVar[bool] = False

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


class GroupMember(Policy):
    """Group-membership gate: outcome selected by the principal's group membership.

    The principal-level (not per-row) counterpart of :class:`Owner`: if the
    caller belongs to at least one of a permission's :attr:`group_ids`, the
    reduction for the action is ``on_match``; otherwise it is ``on_mismatch``.
    ``create`` (which has no existing row to scope) always uses ``on_create``.

    Membership is **pre-bound**: a store-backed resolver resolves the
    principal's groups once per request and calls
    :func:`bind_member_groups` on each resolved policy, which binds the
    principal's membership onto ``GroupMember``'s :attr:`Policy.group_ids`. The
    reduction is therefore pure and storage-free. A ``GroupMember`` whose
    ``group_ids`` is empty (never bound, or a permission with no group) reduces
    to ``on_mismatch`` — fail-closed. All three outcomes default to
    :class:`DenyAll`, so a bare ``GroupMember()`` grants nothing.

    Example::

        GroupMember(group_ids=[moderator_group], on_match=AllowAll())
            # members read/write everything; others denied; no create.

    Its outcome may itself be a caller-scoped policy (e.g. ``Owner``), so it
    keeps the safe caller-scoped default (:attr:`Policy.scopes_to_caller`) and a
    response under it is marked private rather than optimistically shared.

    ``group_ids`` means different things at two moments, deliberately kept
    separate:

    * **as stored**, it names the groups the permission *targets* (what an admin
      declares);
    * **as bound** by :func:`bind_member_groups`, the resolver replaces it with
      the *principal's* groups (via :func:`bind_member_groups`), and the reduction
      then answers "does the principal have a non-empty membership?". The
      principal's membership never overwrites the stored value in the database —
      the binding is on the in-memory copy the resolver yields.
    """

    on_match: Policy = Field(default_factory=DenyAll)
    on_mismatch: Policy = Field(default_factory=DenyAll)
    on_create: Policy = Field(default_factory=DenyAll)

    _member_groups: frozenset[uuid.UUID] = PrivateAttr(default_factory=frozenset)

    @property
    def member_groups(self) -> frozenset[uuid.UUID]:
        """The principal's bound groups (never a stored field).

        Kept off the JSON surface entirely: the resolved membership is
        process-local, bound per request, and never persisted, so it must not
        appear in ``model_dump`` (the ``permission`` column is JSON).
        """
        return self._member_groups

    async def to_search_filter(
        self, user_id: uuid.UUID | None, action: Action
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return await self.on_create.to_search_filter(user_id, action)
        # Membership is checked against the *bound* member groups, so a caller
        # who is a member of a targeted group gets ``on_match``. An unbound copy
        # (empty membership) reduces to ``on_mismatch`` — fail-closed.
        if not self._member_groups.isdisjoint(self.group_ids):
            return await self.on_match.to_search_filter(user_id, action)
        return await self.on_mismatch.to_search_filter(user_id, action)


def bind_member_groups(policy: Policy, groups: frozenset[uuid.UUID]) -> Policy:
    """Bind a principal's group membership onto a membership-aware policy.

    A :class:`GroupMember` target may be *either* a specific group (the stored
    ``group_ids``) *or* "any group the principal belongs to". This binds the
    principal's membership onto the in-memory copy (never the stored value), so
    the pure reduction can answer "is the caller a member?" by intersecting the
    two. A non-membership policy is returned unchanged.
    """
    if isinstance(policy, GroupMember):
        bound = policy.model_copy()
        bound._member_groups = groups
        return bound
    return policy


class Acl(Policy):
    """Portable object-id-list policy — the materialized-ACL escape hatch.

    A bounded set of identifiers the principal may touch, reduced to
    ``AttrFilter(<id_field>, InFilter((...)))`` for the non-create actions (a
    single ``IN`` predicate, not a ``K``-term ``OR``). This is the portable
    flavour of a per-object grant: it travels in the policy tree and works on
    every backend. Beyond the set cap
    (:data:`~resourcey.v2.util.search_filter.MAX_IN_VALUES`) a genuine
    per-object ACL must be pushed down as a store join / subquery instead — see
    :mod:`resourcey.v2.auth.auth_rbac_resolver`.

    ``create`` is unscoped (a new row has no id yet), so it is grantable like
    :class:`Owner`'s create; use ``on_create`` to deny it if desired.

    Attributes:
        ids: The permitted object identifiers.
        id_field: The DTO field naming the row's identifier (``id`` by default).
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
