"""The ``UserPermission`` resource (example 03).

One row per (user, resource_type, policy). The ``permission`` column stores a
serialized :class:`~resourcey.auth.permission.Permission` discriminated-union
object as JSON; the :class:`~resourcey.auth.permission_resolver.PermissionResolver`
deserializes each row at request time and reduces it to a search filter.

Like ``User``, this resource generates its own model for the
``user_permissions`` table on ``ResourceyBase``; the auth layer's
:class:`~resourcey.auth.auth_models.UserPermission` (on ``AuthBase``) maps to
the same physical table. The two declare identical columns, so the merged
migration creates a single table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.util.search_filter import BaseSearchFilter
from sqlalchemy import JSON, Column, ForeignKey, Uuid

from users_and_permissions._secured import SecuredSqlResource


class UserPermission(SecuredSqlResource):
    """A per-user permission policy for a resource type.

    Fields:
        id: UUID primary key (Python-defaulted ``uuid4``).
        user_id: FK to ``users.id`` (required).
        resource_type: The resource type string policies are keyed by.
        permission: A serialized ``Permission`` policy (a JSON dict).
        created_at / updated_at: Auto-managed timestamps.
    """

    id: Annotated[
        UUID,
        ResourceyField(column=Column("id", Uuid, primary_key=True, default=uuid4, nullable=False)),
    ]
    user_id: Annotated[
        UUID,
        ResourceyField(
            column=Column("user_id", Uuid, ForeignKey("users.id"), nullable=False, index=True)
        ),
    ]
    resource_type: str
    permission: Annotated[
        dict[str, Any],
        ResourceyField(column=Column("permission", JSON, nullable=False)),
    ]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[UserPermissionSearchFilter]:
        """Expose ``user_id__eq`` so a user's permission set is listable."""
        return UserPermissionSearchFilter


_UserPermissionModel = UserPermission.get_sql_alchemy_model()


class UserPermissionSearchFilter(BaseSearchFilter[_UserPermissionModel]):  # type: ignore[valid-type]
    """Filter clauses for ``UserPermission.search``."""

    user_id__eq: UUID | None = None
    resource_type__eq: str | None = None
