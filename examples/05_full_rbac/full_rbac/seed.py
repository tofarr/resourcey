"""Seed the full-RBAC store with users, groups, roles and permissions.

The store is the whole point of this example, so the seed is data, not code: the
same UUIDs the ``.env`` binds API keys to, the groups that carry the roles, and
the :data:`~full_rbac.app.ROLE_PERMISSIONS` rules serialized into
``role_permissions``. Run it once after the migration::

    uv run --env-file .env python -m full_rbac.seed

The tests call :func:`seed` directly against their isolated database.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from full_rbac.app import ADMIN, AUTHOR, DENIED, ROLE_PERMISSIONS, VIEWER
from resourcey.auth.auth_policy import DenyAll
from resourcey.auth.auth_rbac import (
    Group,
    GroupRole,
    GroupUser,
    Role,
    RolePermission,
    User,
    policy_to_json,
)

# The users the example's .env keys are bound to (``APP_API_KEYS_<n>_PRINCIPAL_ID``).
ADMIN_USER = UUID("00000000-0000-0000-0000-0000000000a0")
VIEWER_USER = UUID("00000000-0000-0000-0000-0000000000b0")
AUTHOR_USER = UUID("00000000-0000-0000-0000-0000000000c0")

ADMINS = UUID("00000000-0000-0000-0000-000000000001")
VIEWERS = UUID("00000000-0000-0000-0000-000000000002")
AUTHORS = UUID("00000000-0000-0000-0000-000000000003")

#: ``user id -> group id`` — one group per user keeps the demo readable; the
#: resolver joins through it the same way for any membership graph.
MEMBERSHIPS: dict[UUID, UUID] = {
    ADMIN_USER: ADMINS,
    VIEWER_USER: VIEWERS,
    AUTHOR_USER: AUTHORS,
}


async def seed(maker: async_sessionmaker[AsyncSession]) -> None:
    """Populate the RBAC tables with the example's users, groups, roles, permissions."""
    async with maker() as session:
        session.add_all(
            [
                User(id=ADMIN_USER, email="admin@example.com", username="admin"),
                User(id=VIEWER_USER, email="viewer@example.com", username="viewer"),
                User(id=AUTHOR_USER, email="author@example.com", username="author"),
                Group(id=ADMINS, name=ADMINS.hex),
                Group(id=VIEWERS, name=VIEWERS.hex),
                Group(id=AUTHORS, name=AUTHORS.hex),
            ]
        )
        for user_id, group_id in MEMBERSHIPS.items():
            session.add(GroupUser(id=uuid4(), group_id=group_id, user_id=user_id))

        # One group -> one role (named for the demo); a group may carry several.
        group_roles = {ADMINS: ADMIN, VIEWERS: VIEWER, AUTHORS: AUTHOR}
        role_ids: dict[str, UUID] = {}
        for group_id, role_name in group_roles.items():
            role_id = uuid4()
            role_ids[role_name] = role_id
            session.add(Role(id=role_id, name=role_name))
            session.add(GroupRole(id=uuid4(), group_id=group_id, role_id=role_id))
        # The ``denied`` role is not assigned to any group; it exists so a test
        # can prove a DenyAll permission never overrides a grant.
        denied_id = uuid4()
        role_ids[DENIED] = denied_id
        session.add(Role(id=denied_id, name=DENIED))

        for (role_name, resource), policy in ROLE_PERMISSIONS.items():
            session.add(
                RolePermission(
                    id=uuid4(),
                    role_id=role_ids[role_name],
                    resource=resource,
                    permission=policy_to_json(policy),
                )
            )
        # A DenyAll on ``messages`` for the ``denied`` role, to pair with the
        # AllowAll above and demonstrate the union model (deny does not win).
        session.add(
            RolePermission(
                id=uuid4(),
                role_id=denied_id,
                resource="messages",
                permission=policy_to_json(DenyAll()),
            )
        )
        await session.commit()


def _main() -> None:
    """CLI entry point: seed using the app's configured connection."""
    import os

    from resourcey.sql.session_manager import SqlSessionManager
    from resourcey.sql.sql_config import SqlConfig

    async def run() -> None:
        manager = SqlSessionManager(SqlConfig.get_instance())
        async with manager:
            await seed(await manager.get_session_maker())

    if not os.environ.get("APP_SQL_CONNECTIONS_0_URL"):
        raise RuntimeError(
            "Set APP_SQL_CONNECTIONS_0_URL (the framework does no .env loading). Try "
            "`uv run --env-file .env python -m full_rbac.seed`."
        )
    asyncio.run(run())


if __name__ == "__main__":
    _main()
