"""The stored RBAC resource set and its ORM models (issue #133, Part 3 of the auth roadmap).

Parts 1 (#131) and 2 (#132) made the principal and the role vocabulary exist and
centralized ``Principal -> Policy``. This is the **store-backed** rung: real,
server-authoritative ``User`` / ``Group`` / ``GroupUser`` / ``Role`` /
``GroupRole`` / ``RolePermission`` rows, resolved per request by
:class:`~resourcey.auth.auth_rbac_resolver.RbacPolicyResolver`.

The workflow is **model-first**, like every SQL resource: the ORM models here are
the schema of record and the framework infers the DTO from them.

Tables
------
* ``users`` — the local account.
* ``groups`` / ``group_users`` — a named grouping and its membership.
* ``roles`` / ``group_roles`` — a named role and its assignment to a group.
* ``role_permissions`` — the core RBAC unit: a ``resource`` name + a serialized
  :class:`~resourcey.auth.auth_policy.Policy` stored as JSON.
* ``resource_acls`` — the **materialized ACL** escape hatch for a genuinely
  unbounded set of per-object grants. Prefer a broad role rule; this table decays
  and is invalidation-sensitive.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_policy import Policy
from resourcey.core.resource import Resource
from resourcey.sql.sql_resource import SqlResource


def policy_to_json(policy: Policy) -> dict[str, Any]:
    """A stored policy as a JSON-safe dict for the ``role_permissions`` column.

    A policy's ``UUID`` fields must become strings; pydantic coerces them back
    on the read path.
    """
    return cast(dict[str, Any], json.loads(policy.model_dump_json()))


def utc_now() -> datetime:
    """Default factory for the RBAC timestamp columns."""
    return datetime.now(UTC)


class RbacBase(DeclarativeBase):
    """The declarative base owning the RBAC tables (an app points Alembic at it)."""


class User(RbacBase):
    """A local account. The principal a credential's ``user_id`` / ``sub`` names.

    Subclass it to add columns; the resolver depends only on ``id``.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Read by ``principal_is_active`` when a service is built with
    # ``user_resource=``, so a disabled account is rejected.
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

    At request time the resolver fetches every row matching the principal's
    roles *and the target resource*, deserializes each policy, and OR-combines
    them (the union model — no deny-wins override; an empty set is fail-closed).
    """

    __tablename__ = "role_permissions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    resource: Mapped[str] = mapped_column(String(128), index=True)
    # ``JSONB`` on PostgreSQL because ``SELECT DISTINCT permission`` needs an
    # equality operator plain ``json`` does not have.
    permission: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ResourceAcl(RbacBase):
    """A materialized per-object grant, keyed by ``(principal_id, resource_name)``.

    The escape hatch for a genuinely unbounded set of per-object grants, where
    the permission is evaluated as a join / subquery so the grants never enter
    Python. This table is derived data: a second cache to maintain on write, and
    it inherits the credential-threshold staleness. Prefer a broad role rule
    ("everything I own") whenever possible.
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

    Each model is served by a plain ``SqlResource``, so the whole set can be
    seeded / administered over REST. An app that hides an internal table
    registers a narrowing ``ResourceView`` instead.
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

    A role grant that names every path stays in sync with the served surface.
    """
    return tuple(SqlResource(model).get_resource_path() for model in RBAC_MODELS)
