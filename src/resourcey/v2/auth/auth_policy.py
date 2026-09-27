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

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from resourcey.v2.core.service import Action
from resourcey.v2.util.models import DiscriminatedUnionMixin
from resourcey.v2.util.search_filter import AllFilter, NoMatchFilter, SearchFilter

# The read-like actions a ReadOnly policy grants: the four actions that only
# read. The write actions (create / update / delete / batch_edit) are excluded.
_READ_LIKE_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


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
