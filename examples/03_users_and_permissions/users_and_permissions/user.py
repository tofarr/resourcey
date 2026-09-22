"""The ``User`` resource (example 03).

Mirrors the auth layer's :class:`~resourcey.auth.auth_models.User` ORM model
(email, username, enabled, password, idp_user_id) and adds a ``creator_id``
UUID column so :class:`~resourcey.auth.permission.CreatorPermission` can scope
"edit your own user record" to the principal that created it.

The resource generates its own SQLAlchemy model for the ``users`` table on
:class:`~resourcey.resource.sql.ResourceyBase`; the auth layer's
:class:`~resourcey.auth.auth_models.User` (on ``AuthBase``) maps to the same
physical table. The resource's model is a superset (it adds ``creator_id``),
so the merged migration creates a single ``users`` table with every column
both layers need.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from sqlalchemy import Column, ForeignKey, String, Uuid


class User(SqlResource):
    """A local user.

    Fields:
        id: UUID primary key (Python-defaulted ``uuid4``; SQLite has no
            ``gen_random_uuid()`` server default).
        email: Unique email (required).
        username: Unique username (required).
        enabled: Whether the user can authenticate (default True).
        password: Bcrypt hash (nullable for IdP-only users).
        idp_user_id: Stable IdP subject for callback lookup (nullable).
        creator_id: The user who created this record; auto-stamped from the
            authenticated principal on create so ``CreatorPermission`` can scope
            "edit your own record".
        created_at / updated_at: Auto-managed timestamps.
    """

    id: Annotated[
        UUID,
        ResourceyField(column=Column("id", Uuid, primary_key=True, default=uuid4, nullable=False)),
    ]
    email: str
    username: str
    enabled: bool = True
    password: str | None = None
    idp_user_id: Annotated[
        str | None,
        ResourceyField(column=Column("idp_user_id", String(255), nullable=True, index=True)),
    ] = None
    creator_id: Annotated[
        UUID | None,
        ResourceyField(
            column=Column("creator_id", Uuid, ForeignKey("users.id"), nullable=True, index=True)
        ),
    ] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Eagerly build the ORM model so the ``users`` table lands in metadata before
# migrations or table creation run.
_UserModel = User.get_sql_alchemy_model()
