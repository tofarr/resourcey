"""The stored RBAC resource set and its ORM models (issue #133, Part 3 of the auth roadmap).

Parts 1 (#131) and 2 (#132) made the principal and the role vocabulary exist and
centralized ``Principal -> Policy``. This is the **store-backed** rung: real,
server-authoritative ``User`` / ``Group`` / ``GroupUser`` / ``Role`` /
``GroupRole`` / ``RolePermission`` rows, resolved per request by
:class:`~resourcey.v2.auth.auth_rbac_resolver.RbacPolicyResolver`.

The workflow is **model-first**, like every ``v2`` SQL resource: the ORM models
here are the schema of record and the framework infers the DTO from them. The
model is ported from v1's deferred design
(``resourcey.auth.auth_models`` ``User`` / ``UserPermission``), completed with
the group / role tables v1 shipped only as a placeholder.

Tables
------
* ``users`` — the local account (``id``, ``email``, ``username``, ``enabled``).
* ``groups`` — a named grouping.
* ``group_users`` — group membership (``group_id`` -> ``groups.id``,
  ``user_id`` -> ``users.id``).
* ``roles`` — a named role (the stored counterpart of Part 2's app-level role
  strings).
* ``group_roles`` — role assignment to a group.
* ``role_permissions`` — the core RBAC unit: a ``resource`` name + a serialized
  :class:`~resourcey.v2.auth.auth_policy.Policy` (a discriminated-union value
  stored as JSON).
* ``resource_acls`` — the **materialized ACL** escape hatch: a per-object grant
  keyed by ``(principal_id, resource_name)``, for a genuinely unbounded set of
  object ids. Prefer a broad role rule; this table decays (it is a second cache
  to invalidate on write) and is invalidation-sensitive. See
  :class:`~resourcey.v2.auth.auth_rbac_store.SqlRbacStore`.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.auth.auth_policy import Policy
from resourcey.v2.core.resource import Resource
from resourcey.v2.sql.sql_resource import SqlResource


def policy_to_json(policy: Policy) -> dict[str, Any]:
    """A stored policy as a JSON-safe dict for the ``role_permissions`` column.

    The ``permission`` column is JSON, so a policy's ``UUID`` fields (``group_ids``,
    ``Acl.ids``) must become strings. ``model_dump_json`` handles that, and
    ``json.loads`` turns the result back into the dict SQLAlchemy's JSON type
    stores; pydantic coerces the strings back to UUIDs on the read path.
    """
    return cast(dict[str, Any], json.loads(policy.model_dump_json()))


def utc_now() -> datetime:
    """Default factory for the RBAC timestamp columns."""
    return datetime.now(UTC)


class RbacBase(DeclarativeBase):
    """The declarative base owning the RBAC tables.

    Kept local so the auth package is self-contained; an app points Alembic at
    this metadata (or imports the models) exactly as it does for any SQLAlchemy
    model.
    """


class User(RbacBase):
    """A local account. The principal a credential's ``user_id`` / ``sub`` names.

    Extensible per app, like v1's ``User``: subclass it and add columns; the
    resolver depends only on ``id``.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Stored for administration, but not yet consulted on the auth path: an API
    # key is validated on its own row, so ``enabled=False`` does not by itself
    # revoke access. Wiring that (a check when a key's owner is resolved) is a
    # later rung.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Group(RbacBase):
    """A named grouping of users."""

    __tablename__ = "groups"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class GroupUser(RbacBase):
    """Group membership: ``user_id`` is a member of ``group_id``."""

    __tablename__ = "group_users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Role(RbacBase):
    """A named role — the stored counterpart of Part 2's app-level role strings."""

    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class GroupRole(RbacBase):
    """Role assignment: ``role_id`` is granted to ``group_id``."""

    __tablename__ = "group_roles"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RolePermission(RbacBase):
    """The core RBAC unit: a ``resource`` name + a serialized ``Policy``.

    ``permission`` holds an :meth:`~pydantic.BaseModel.model_dump` of a
    :class:`~resourcey.v2.auth.auth_policy.Policy` discriminated union (e.g.
    ``{"kind": "Owner", "owner_field": "author_id"}``). At request time the
    resolver fetches every row matching the principal's roles *and the target
    resource*, deserializes each policy, and OR-combines them (the union model —
    no deny-wins override; an empty set is fail-closed).
    """

    __tablename__ = "role_permissions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    resource: Mapped[str] = mapped_column(String(128), index=True)
    # ``JSONB`` on PostgreSQL: the resolver de-duplicates with ``SELECT DISTINCT
    # permission``, which needs an equality operator plain ``json`` does not have.
    # ``with_variant`` keeps the portable ``JSON`` everywhere else (SQLite tests).
    permission: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ResourceAcl(RbacBase):
    """A materialized per-object grant, keyed by ``(principal_id, resource_name)``.

    The documented high-end escape hatch: when a principal holds a genuinely
    unbounded set of per-object grants, enumerate them here and evaluate the
    permission as a *join / subquery* (``WHERE id IN (SELECT resource_id FROM
    resource_acls WHERE principal_id = ... AND resource_name = ...)``) so the
    grants never enter Python. This table is derived data: it is a second cache
    to maintain on write and it inherits the credential-threshold staleness.
    Choose it only when per-object grants are truly unbounded; prefer a broad
    role rule ("everything I own") whenever possible.
    """

    __tablename__ = "resource_acls"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    principal_id: Mapped[UUID] = mapped_column(index=True)
    resource_name: Mapped[str] = mapped_column(String(128), index=True)
    resource_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# Public aliases so a caller imports the model by its resource name.
RBAC_MODELS: tuple[type[Any], ...] = (
    User,
    Group,
    GroupUser,
    Role,
    GroupRole,
    RolePermission,
    ResourceAcl,
)


def rbac_resources(
    *,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
) -> list[Resource[Any, Any]]:
    """The full RBAC resource set, wired to one session source.

    Each model is served by a plain :class:`~resourcey.v2.sql.sql_resource.SqlResource`
    (model-first), so the whole set is exposed over the ordinary REST surface and
    can be seeded / administered through it. An app that wants to hide an
    internal table registers a narrowing
    :class:`~resourcey.v2.view.resource_view.ResourceView` instead.
    """
    return [
        SqlResource(
            model,
            session_factory=session_factory,
            session_manager=session_manager,
            name=name,
        )
        for model in RBAC_MODELS
    ]


def rbac_resource_paths() -> tuple[str, ...]:
    """The REST paths :func:`rbac_resources` serves, in the same order.

    A caller granting a role access to the RBAC tables (an admin) must name every
    path, so deriving them here keeps a role grant from silently drifting out of
    sync with the served surface.
    """
    return tuple(SqlResource(model).get_resource_path() for model in RBAC_MODELS)
