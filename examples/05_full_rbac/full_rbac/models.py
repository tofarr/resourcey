"""SQLAlchemy models for the full-RBAC example.

The ``v2`` SQL workflow is **model-first**, so the ORM models are the schema of
record and the framework infers the DTO (and REST models) from them. This
example combines two model sets in one migration:

* the board — ``Thread`` and ``Message`` — exactly as in example 04;
* the framework's stored RBAC tables (``users`` / ``groups`` / ``group_users`` /
  ``roles`` / ``group_roles`` / ``role_permissions`` / ``resource_acls``),
  re-exported from :mod:`resourcey.v2.auth.auth_rbac`.

``Message.author_id`` is the owner column the ``Owner`` (creator) policy scopes
on; it is server-stamped, never client-supplied, so it carries a
:class:`~resourcey.v2.core.dto.DtoField` in the column's ``info``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.auth.auth_rbac import (  # noqa: F401  (re-exported for seeding / the migration)
    Group,
    GroupRole,
    GroupUser,
    RbacBase,
    ResourceAcl,
    Role,
    RolePermission,
    User,
)
from resourcey.v2.core.dto import DtoField

OWNER_FIELD = DtoField(in_create_request=False, in_update_request=False)


def _utc_now() -> datetime:
    """Default / onupdate factory for the timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the example's own tables (``threads`` / ``messages``)."""


class Thread(Base):
    """A shared thread — every reader may see the whole board."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


class Message(Base):
    """A message owned by ``author_id`` — the ``Owner`` policy scopes it per row."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("threads.id"), nullable=False, index=True
    )
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True, info={"dto_field": OWNER_FIELD}
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


# The RBAC tables live in the framework's own ``RbacBase`` metadata; fold them
# into this example's metadata so one Alembic diff (and one ``create_all``)
# covers the whole schema.
for _table in RbacBase.metadata.tables.values():
    if _table.key not in Base.metadata.tables:
        _table.to_metadata(Base.metadata)
