"""Permission resolver: reduce stored policies to a search filter (issue #4).

The :class:`PermissionResolver` bridges the stored
:class:`~resourcey.auth.auth_models.UserPermission` rows and the
:class:`~resourcey.auth.secured_service.SecuredService` wrapper.

At request time the resolver:

1. Queries every ``UserPermission`` row matching ``(user_id, resource_type)``.
2. Deserializes each row's ``permission`` JSON into a
   :class:`~resourcey.auth.permission.Permission` discriminated-union object.
3. Reduces each policy to a :class:`~resourcey.util.search_filter.SearchFilter`
   via :meth:`~resourcey.auth.permission.Permission.to_search_filter` for the
   requested ``(user_id, action, groups)`` triple.
4. OR-combines all reduced filters (union model — no deny-wins override).

If no policies match, the resolver returns ``None`` (fail-closed) — a resource
with no configured permissions is inaccessible rather than open. Applications
that want an open-by-default resource can register a default
:class:`~resourcey.auth.permission.Permitted` policy for ``user_id=None``.

Default permissions
-------------------

An app can declare default policies that apply to every principal (including
anonymous) via :class:`DefaultPermissions`. These are merged with the
DB-stored user permissions: the combined filter is ``OR(user_policies,
default_policies)``. Defaults are evaluated without a DB round-trip, so they
are useful for bootstrapping access before any ``UserPermission`` rows exist.

Example::

    defaults = DefaultPermissions({
        "document": [CreatorPermission(on_match=Permitted())],
        "public_post": [ReadOnly()],
    })
    resolver = PermissionResolver(session, defaults=defaults)

    secured = SecuredService(
        inner=sql_service,
        resource_type="document",
        resource_name="document",
        user_id=current_user_id,
        groups=frozenset(),
        resolver=resolver.resolve,
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth.auth_models import UserPermission
from resourcey.auth.permission import Permission
from resourcey.resource.service_base import Action
from resourcey.util.search_filter import (
    NONE,
    SearchFilter,
    or_filter,
)

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class DefaultPermissions:
    """App-level default permission policies per resource type.

    Policies listed under a resource type apply to **every** principal
    (including anonymous), and are OR-combined with the principal's
    DB-stored user permissions. This lets an app bootstrap access before any
    ``UserPermission`` rows exist — e.g. grant ``CreatorPermission`` on every
    resource by default, or make a ``public_*`` resource read-only to all.

    Example::

        DefaultPermissions({
            "document": [CreatorPermission(on_match=Permitted())],
            "public_post": [ReadOnly()],
        })
    """

    policies: dict[str, list[Permission]] = field(default_factory=dict)

    def for_resource(self, resource_type: str) -> list[Permission]:
        """Return the default policies for *resource_type* (empty if none)."""
        return self.policies.get(resource_type, [])

    def add(self, resource_type: str, *policies: Permission) -> DefaultPermissions:
        """Return a copy with *policies* appended to *resource_type*'s defaults."""
        existing = list(self.policies.get(resource_type, []))
        existing.extend(policies)
        new_map = {**self.policies, resource_type: existing}
        return DefaultPermissions(new_map)

    @classmethod
    def from_config(cls, raw: dict[str, list[dict[str, Any]]]) -> DefaultPermissions:
        """Build from the ``AuthConfig.default_permissions`` config field.

        Each value is a list of serialized :class:`Permission` discriminated-union
        objects (``{"kind": "permitted"}`` etc.). Unrecognized entries are
        skipped (fail-soft for config typos).
        """
        policies: dict[str, list[Permission]] = {}
        for resource_type, raw_list in raw.items():
            parsed: list[Permission] = []
            for raw_policy in raw_list:
                try:
                    parsed.append(Permission.model_validate(raw_policy))
                except Exception:
                    continue
            if parsed:
                policies[resource_type] = parsed
        return cls(policies)


class PermissionResolver:
    """Resolve the effective permission filter for a resource + action + principal.

    Holds an :class:`~sqlalchemy.ext.asyncio.AsyncSession` for querying
    ``UserPermission`` rows and optional
    :class:`DefaultPermissions` for app-level defaults. The resolver is
    constructed per-request (the session is request-scoped); the
    :meth:`resolve` method has the signature expected by
    :class:`~resourcey.auth.secured_service.SecuredService`'s ``resolver``
    parameter.

    When the session is ``None`` (or not provided), only default policies are
    evaluated — useful for tests and pre-DB bootstrap.
    """

    def __init__(
        self,
        session: AsyncSession | None,
        *,
        defaults: DefaultPermissions | None = None,
    ) -> None:
        self._session = session
        self._defaults = defaults or DefaultPermissions()

    async def resolve(
        self,
        resource_type: str,
        action: Action,
        user_id: uuid.UUID | None,
        groups: frozenset[uuid.UUID] = frozenset(),
    ) -> SearchFilter[Any] | None:
        """Compute the effective permission filter, or ``None`` (fail-closed).

        Combines default policies and DB-stored user policies via OR. Returns
        ``None`` when no policy applies (neither defaults nor user rows), so
        a resource with no configured permissions is inaccessible.
        """
        filters: list[SearchFilter[Any]] = []

        # Default policies (no DB needed) — apply to every principal.
        for policy in self._defaults.for_resource(resource_type):
            filters.append(policy.to_search_filter(user_id, action, groups))

        # DB-stored user policies — only when a session is available and the
        # principal is authenticated. Anonymous principals (user_id is None)
        # have no stored policies.
        if self._session is not None and user_id is not None:
            user_filters = await self._resolve_user_policies(
                resource_type,
                action,
                user_id,
                groups,
            )
            filters.extend(user_filters)

        if not filters:
            return None

        combined = or_filter(*filters)
        # or_filter of all-None children yields NONE (empty disjunction =
        # false). But an empty list is handled above (return None). If every
        # policy reduced to NONE, the combined filter is NONE — which is a
        # valid "deny" (not "no policy"). The SecuredService treats NONE as
        # deny, and None as deny (fail-closed), so both paths are deny.
        if combined is NONE:
            return NONE
        return combined

    async def _resolve_user_policies(
        self,
        resource_type: str,
        action: Action,
        user_id: uuid.UUID,
        groups: frozenset[uuid.UUID],
    ) -> list[SearchFilter[Any]]:
        """Fetch and reduce the user's DB-stored policies for *resource_type*."""
        assert self._session is not None
        stmt = select(UserPermission).where(
            UserPermission.user_id == user_id,
            UserPermission.resource_type == resource_type,
        )
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        filters: list[SearchFilter[Any]] = []
        for row in rows:
            try:
                policy = Permission.model_validate(row.permission)
            except Exception:
                # A corrupt/unrecognized policy in the DB is skipped rather
                # than crashing the request. Log in production; here we
                # silently drop it so one bad row doesn't deny everything.
                continue
            filters.append(policy.to_search_filter(user_id, action, groups))
        return filters


def make_resolver(
    session: AsyncSession | None = None,
    *,
    defaults: DefaultPermissions | None = None,
) -> PermissionResolver:
    """Convenience factory for a :class:`PermissionResolver`."""
    return PermissionResolver(session, defaults=defaults)


async def depends_permission_resolver(
    request: Any,
    session: AsyncSession | None = None,
) -> PermissionResolver:
    """FastAPI dependency: build a :class:`PermissionResolver` for the request.

    Reads default permissions from the active :class:`FrameworkConfig` and
    uses the request's session (opened by :mod:`resourcey.auth.session` or
    a resource's ``get_service_dependency``). Falls back to ``None`` session
    (defaults-only mode) when no session is available.
    """
    from resourcey.config.config_framework import FrameworkConfig
    from resourcey.config.config_runtime import get_config_as

    cfg = get_config_as(FrameworkConfig)
    defaults = DefaultPermissions.from_config(cfg.auth.default_permissions)

    existing = getattr(request.state, "session", None)
    if existing is not None:
        return PermissionResolver(existing, defaults=defaults)
    if session is not None:
        return PermissionResolver(session, defaults=defaults)
    return PermissionResolver(None, defaults=defaults)
