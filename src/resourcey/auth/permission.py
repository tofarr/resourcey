"""The polymorphic permission policy object (issue #4).

A :class:`Permission` is a discriminated-union (``DiscriminatedUnionMixin``)
Pydantic model: a concrete permission policy that knows how to reduce itself to
a :class:`~resourcey.util.search_filter.SearchFilter` for a given
``(user_id, action, groups)`` triple. Storing the policy itself (rather than a
serialized filter) keeps the decision logic co-located with the data and lets
the same stored policy adapt as the requested action changes.

This is modelled closely on ohev2's ``security_models`` layer, but reuses
resourcey's own :class:`~resourcey.resource.service_base.Action` (which has
``COUNT`` and ``BATCH_*`` members ohev2's security ``Action`` lacks, and no
``USE``). Policies that don't distinguish ``COUNT`` / ``BATCH_*`` reduce them
to their closest CRUD action via :func:`normalize_action`.

Built-in policies:

* :class:`Permitted`         — always grants full access (``AllSearchFilter``).
* :class:`Denied`            — always denies access (``NoneSearchFilter``).
* :class:`ReadOnly`          — grants read/search/count, denies everything else.
* :class:`AclPermission`     — item-id-list policy with per-outcome sub-policies.
* :class:`CreatorPermission` — creator-ownership policy (``creator_id == user_id``).
* :class:`GroupPermission`   — group-membership policy (principal in a group set).

The union (OR) model: a principal's effective permission for a resource is the
OR-combination of every matching policy's reduced filter. A :class:`Denied`
policy reduces to :class:`NoneSearchFilter`, which contributes nothing to the
OR — it does **not** override other grants. There is no deny-wins override.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, cast

from pydantic import Field, field_validator
from sqlalchemy import and_, column, false, or_
from sqlalchemy.sql.elements import ColumnElement

from resourcey.resource.service_base import Action
from resourcey.util.models import DiscriminatedUnionMixin
from resourcey.util.search_filter import (
    AllSearchFilter,
    NoneSearchFilter,
    SearchFilter,
    T,
)

#: Maximum number of item ids allowed in an :class:`AclPermission`. Forces
#: complex/large-scale grants to use custom permission classes (which can
#: express dynamic scoping like "all users with role X") instead of
#: enumerating ids that would go stale.
ACL_MAX_IDS: int = 100


def normalize_action(action: Action) -> Action:
    """Reduce ``COUNT`` / ``BATCH_*`` to their closest CRUD action.

    ``COUNT`` reuses the ``SEARCH`` permission (counting is not a separate
    privilege — matches the ``resource_actions`` spec). ``BATCH_READ`` reuses
    ``READ`` and ``BATCH_EDIT`` reuses ``UPDATE``. Policies that don't care
    about these members can branch on the normalized action alone.
    """
    if action is Action.COUNT:
        return Action.SEARCH
    if action is Action.BATCH_READ:
        return Action.READ
    if action is Action.BATCH_EDIT:
        return Action.UPDATE
    return action


class Permission(DiscriminatedUnionMixin, ABC):
    """Abstract base for a permission policy.

    Concrete subclasses participate in the SDK discriminated-union machinery
    (a ``kind`` computed field tags the concrete type) so a stored policy can be
    serialized to JSON and deserialized back to the right subclass. Subclasses
    implement :meth:`to_search_filter`, which reduces the policy to a
    :class:`SearchFilter` for the given ``(user_id, action, groups)`` triple.

    ``groups`` is the set of group ids the current principal is a member of
    (resolved once per request by the auth dependency layer and passed to every
    policy reduction). Policies that do not depend on group membership ignore
    it; :class:`GroupPermission` uses it to pick its outcome.
    """

    @abstractmethod
    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        """Reduce this policy to a search filter for ``(user_id, action, groups)``.

        A returned :class:`NoneSearchFilter` means "deny" (no rows visible / no
        create allowed); an :class:`AllSearchFilter` means the whole resource
        table is in scope.
        """
        raise NotImplementedError


class Permitted(Permission):
    """Policy that always grants full, unrestricted access."""

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        return AllSearchFilter[Any]()


class Denied(Permission):
    """Policy that always denies access (matches no rows).

    Under the union model this reduces to :class:`NoneSearchFilter`, which
    contributes nothing to the OR-combination — it does not override other
    grants.
    """

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        return NoneSearchFilter[Any]()


class ReadOnly(Permission):
    """Policy that grants read/search/count and denies all other actions."""

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        if normalize_action(action) in (Action.READ, Action.SEARCH):
            return AllSearchFilter[Any]()
        return NoneSearchFilter[Any]()


class AclFilter(SearchFilter[T]):
    """Filter admitting items whose ``id`` is in a fixed set.

    The SQL condition uses an unqualified ``id`` column reference, which is
    correct for the single-table ``select(Entity)`` statements that services
    build before applying the permission filter.

    An empty ``ids`` list means "no restriction" (matches every row): the SQL
    condition is ``None``. Callers that need "match nothing" for an empty set
    must use :class:`NoneSearchFilter` instead.
    """

    ids: list[uuid.UUID]

    def matches(self, item: T) -> bool:
        if not self.ids:
            return True
        return getattr(item, "id", None) in self.ids

    def sql_condition(self) -> ColumnElement[bool] | None:
        if not self.ids:
            return None  # no ids -> no restriction added (deny handled by caller)
        return column("id").in_(self.ids)

    def negated_sql_condition(self) -> ColumnElement[bool] | None:
        if not self.ids:
            return false()
        return column("id").notin_(self.ids)


class CreatorMatchFilter(SearchFilter[T]):
    """Filter admitting items whose ``creator_id`` equals a fixed principal.

    Items with a ``NULL`` ``creator_id`` never match (``creator_id = x`` is
    false for ``NULL``), so ownership-scoped grants do not admit unowned rows.
    """

    creator_id: uuid.UUID

    def matches(self, item: T) -> bool:
        return getattr(item, "creator_id", None) == self.creator_id

    def sql_condition(self) -> ColumnElement[bool] | None:
        return column("creator_id") == self.creator_id

    def negated_sql_condition(self) -> ColumnElement[bool] | None:
        # NULL-safe complement: ``creator_id IS DISTINCT FROM x`` admits rows
        # whose creator is NULL (so unowned items fall to on_mismatch) and rows
        # owned by another principal. ``creator_id != x`` would be false for
        # NULL and drop unowned items from both branches.
        return column("creator_id").is_distinct_from(self.creator_id)


class _ScopeMatchFilter(SearchFilter[T]):
    """Two-branch filter: items in *match* get *in_scope*; the rest get *out_scope*.

    Built by :class:`AclPermission` and :class:`CreatorPermission` to express
    "items satisfying a match predicate get one policy, everything else gets
    another" without compositing dozens of AND/OR wrappers. ``match``,
    ``in_scope`` and ``out_scope`` are runtime :class:`SearchFilter` objects
    (never persisted), so child dicts are resolved via the unparameterized base
    for parity with the composite filters.
    """

    # Typed ``Any`` (not ``SearchFilter[Any]``) so Pydantic does not route
    # nested values through the discriminated-union validator at field-assignment
    # time — the same convention used by ``AndSearchFilter`` / ``OrSearchFilter``.
    match: Any
    in_scope: Any
    out_scope: Any

    @field_validator("match", "in_scope", "out_scope", mode="before")
    @classmethod
    def _resolve_child(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return SearchFilter.model_validate(value)
        return value

    def matches(self, item: T) -> bool:
        match_f = cast("SearchFilter[Any]", self.match)
        if match_f.matches(item):
            return cast("SearchFilter[Any]", self.in_scope).matches(item)
        return cast("SearchFilter[Any]", self.out_scope).matches(item)

    def sql_condition(self) -> ColumnElement[bool] | None:
        match_f = cast("SearchFilter[Any]", self.match)
        match_cond = match_f.sql_condition()
        in_cond = cast("SearchFilter[Any]", self.in_scope).sql_condition()
        out_cond = cast("SearchFilter[Any]", self.out_scope).sql_condition()

        # match_cond is None only when match admits every row (e.g. empty
        # AclFilter). In that case the out-of-scope branch never applies.
        if match_cond is None:
            return in_cond

        left = match_cond if in_cond is None else and_(match_cond, in_cond)
        # Use the match filter's NULL-safe complement (e.g. IS DISTINCT FROM
        # for creator equality) so rows the match predicate cannot see —
        # including NULL creator_id rows — fall to the out-of-scope branch.
        negated = match_f.negated_sql_condition()
        if negated is None:
            return left
        right = negated if out_cond is None else and_(negated, out_cond)
        return or_(left, right)


def _reduce_outcome(
    outcome: Permission,
    user_id: uuid.UUID | None,
    action: Action,
    groups: frozenset[uuid.UUID],
) -> SearchFilter[Any]:
    """Reduce a sub-policy outcome to its search filter for the triple.

    A :class:`Denied` outcome (the default) reduces to :class:`NoneSearchFilter`,
    which :class:`AclPermission` / :class:`CreatorPermission` rely on to make
    an unset branch contribute nothing.
    """
    return outcome.to_search_filter(user_id, action, groups)


class AclPermission(Permission):
    """Item-id-list policy with per-outcome sub-policies.

    Splits a resource's items into two sets — those whose ``id`` is in
    ``item_ids`` (the "in-list" set) and the rest — and applies a separate
    sub-policy to each set:

    * in-list items for non-CREATE actions  -> ``on_match``
    * out-of-list items for non-CREATE      -> ``on_mismatch``
    * CREATE (regardless of any prospective id) -> ``on_create``

    All three outcomes default to :class:`Denied`, so a bare
    ``AclPermission(item_ids=[...])`` grants nothing — each grant must be
    explicit. Examples::

        AclPermission(item_ids=[a, b], on_match=Permitted())
            # full access to a, b; everything else denied; no create.
        AclPermission(item_ids=[a, b], on_match=ReadOnly())
            # read/search a, b; no mutations; no create.
        AclPermission(item_ids=[a, b], on_match=Permitted(),
                      on_create=Permitted(), on_mismatch=ReadOnly())
            # full access to a, b; create allowed; everything else read-only.

    For non-CREATE actions the effective filter is::

        OR( AND(id IN item_ids, on_match), AND(id NOT IN item_ids, on_mismatch) )

    so an in-list item is admitted iff ``on_match`` admits it for the action
    and an out-of-list item iff ``on_mismatch`` does. When ``item_ids`` is
    empty every item is out-of-list, so the filter is just ``on_mismatch``.
    """

    item_ids: list[uuid.UUID] = Field(default_factory=list)
    on_match: Permission = Field(default_factory=Denied)
    on_mismatch: Permission = Field(default_factory=Denied)
    on_create: Permission = Field(default_factory=Denied)

    @field_validator("item_ids")
    @classmethod
    def _enforce_id_cap(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) > ACL_MAX_IDS:
            raise ValueError(
                f"AclPermission permits at most {ACL_MAX_IDS} item ids; got "
                f"{len(value)}. Use a custom permission class for larger grants."
            )
        return value

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return _reduce_outcome(self.on_create, user_id, action, groups)
        if not self.item_ids:
            # Nothing is in-list; every item falls to on_mismatch.
            return _reduce_outcome(self.on_mismatch, user_id, action, groups)
        match = AclFilter[Any](ids=list(self.item_ids))
        in_scope = _reduce_outcome(self.on_match, user_id, action, groups)
        out_scope = _reduce_outcome(self.on_mismatch, user_id, action, groups)
        return _ScopeMatchFilter[Any](match=match, in_scope=in_scope, out_scope=out_scope)


class CreatorPermission(Permission):
    """Creator-ownership policy (``creator_id == user_id``).

    Splits a resource's items into those the current principal created
    (``creator_id == user_id``) and the rest, applying a sub-policy to each:

    * own items for non-CREATE actions -> ``on_match``
    * others' items for non-CREATE    -> ``on_mismatch``
    * CREATE                          -> ``on_create``

    All three default to :class:`Denied`. Anonymous principals
    (``user_id is None``) can never be a creator, so non-CREATE actions reduce
    to ``on_mismatch`` and CREATE to ``on_create`` (which defaults to deny).

    Example::

        CreatorPermission(on_match=Permitted(), on_mismatch=ReadOnly())
            # full access to own items; read-only access to others'; no create.
    """

    on_match: Permission = Field(default_factory=Denied)
    on_mismatch: Permission = Field(default_factory=Denied)
    on_create: Permission = Field(default_factory=Denied)

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return _reduce_outcome(self.on_create, user_id, action, groups)
        if user_id is None:
            # Anonymous can never match a creator; everything is out-of-scope.
            return _reduce_outcome(self.on_mismatch, user_id, action, groups)
        match = CreatorMatchFilter[Any](creator_id=user_id)
        in_scope = _reduce_outcome(self.on_match, user_id, action, groups)
        out_scope = _reduce_outcome(self.on_mismatch, user_id, action, groups)
        return _ScopeMatchFilter[Any](match=match, in_scope=in_scope, out_scope=out_scope)


class GroupPermission(Permission):
    """Group-membership policy (principal is in one of ``group_ids``).

    A principal-level gate rather than an item-level split: if the current
    principal is a member of at least one of ``group_ids`` the effective filter
    is ``on_match`` reduced for the action; otherwise it is ``on_mismatch``.
    CREATE always uses ``on_create``. All three default to :class:`Denied`.

    Membership is resolved once per request by the auth dependency layer
    (which queries group memberships) and passed in via the ``groups``
    argument, so :meth:`to_search_filter` stays synchronous and DB-free.

    .. note::
        Group storage (``Group`` / ``GroupUser``) is deferred to a follow-up
        issue; until then ``groups`` is always empty and this policy reduces
        to ``on_mismatch`` for non-CREATE actions. It is shipped now so the
        policy vocabulary is complete and forward-compatible.

    Example::

        GroupPermission(group_ids=[g], on_match=Permitted())
            # members of g get full access; non-members denied; no create.
    """

    group_ids: list[uuid.UUID] = Field(default_factory=list)
    on_match: Permission = Field(default_factory=Denied)
    on_mismatch: Permission = Field(default_factory=Denied)
    on_create: Permission = Field(default_factory=Denied)

    def to_search_filter(
        self,
        user_id: uuid.UUID | None,
        action: Action,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return _reduce_outcome(self.on_create, user_id, action, groups)
        outcome = self.on_match if not groups.isdisjoint(self.group_ids) else self.on_mismatch
        return _reduce_outcome(outcome, user_id, action, groups)
