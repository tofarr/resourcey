"""Seed the full-RBAC store with users, groups, roles, permissions and API keys.

The store is the whole point of this example, so the seed is data, not code: the
users' UUIDs, the groups that carry the roles, the
:data:`~full_rbac.app.ROLE_PERMISSIONS` rules serialized into
``role_permissions``, and the API keys bound to those users. Run it once after
the migration::

    uv run --env-file .env python -m full_rbac.seed

The tests call :func:`seed` directly against their isolated database.

**Keys.** The plaintext keys live in :data:`RAW_KEYS` — this file is the only
place they exist, which is why a committed dev key is acceptable here. The store
receives their SHA-256 **digest**
(:func:`~resourcey.auth.auth_api_key_resource.hash_api_key`), which is what a
presented key is hashed to and looked up by, so the table never holds a usable
credential.

**Idempotent.** Every row is inserted only when it is absent, so re-running the
seed is a no-op. "Re-seed" is the natural response to a confusing empty result,
and a seed that failed there would compound the confusion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from full_rbac.app import ADMIN, AUTHOR, DENIED, ROLE_PERMISSIONS, VIEWER
from resourcey.auth.auth_api_key_resource import ApiKey, hash_api_key
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

# The users the example's keys are bound to (the ``api_keys.user_id`` column).
ADMIN_USER = UUID("00000000-0000-0000-0000-0000000000a0")
VIEWER_USER = UUID("00000000-0000-0000-0000-0000000000b0")
AUTHOR_USER = UUID("00000000-0000-0000-0000-0000000000c0")

ADMINS = UUID("00000000-0000-0000-0000-000000000001")
VIEWERS = UUID("00000000-0000-0000-0000-000000000002")
AUTHORS = UUID("00000000-0000-0000-0000-000000000003")

# The role names. Stored role ids are derived from these, so a re-seed lands on
# the same rows; an id already present under the name is reused as-is.
ROLE_NAMES: tuple[str, ...] = (ADMIN, VIEWER, AUTHOR, DENIED)

#: ``user id -> group id`` — one group per user keeps the demo readable; the
#: resolver joins through it the same way for any membership graph.
MEMBERSHIPS: dict[UUID, UUID] = {
    ADMIN_USER: ADMINS,
    VIEWER_USER: VIEWERS,
    AUTHOR_USER: AUTHORS,
}

#: ``group id -> role name`` — a group may carry several roles.
GROUP_ROLES: dict[UUID, str] = {ADMINS: ADMIN, VIEWERS: VIEWER, AUTHORS: AUTHOR}

#: ``user id -> plaintext API key``. Only the digest of these is stored; the
#: plaintexts are what the README's ``curl`` examples send.
RAW_KEYS: dict[UUID, str] = {
    ADMIN_USER: "admin-key",
    VIEWER_USER: "viewer-key",
    AUTHOR_USER: "author-key",
}

def _users() -> tuple[User, ...]:
    """Fresh ``User`` instances — never module-level, or a second seed would
    hand SQLAlchemy objects it already considers persistent and skip the INSERT."""
    return (
        User(id=ADMIN_USER, email="admin@example.com", username="admin"),
        User(id=VIEWER_USER, email="viewer@example.com", username="viewer"),
        User(id=AUTHOR_USER, email="author@example.com", username="author"),
    )


def _groups() -> tuple[Group, ...]:
    """Fresh ``Group`` instances, for the same reason as :func:`_users`."""
    return (
        Group(id=ADMINS, name=ADMINS.hex),
        Group(id=VIEWERS, name=VIEWERS.hex),
        Group(id=AUTHORS, name=AUTHORS.hex),
    )


async def seed(maker: async_sessionmaker[AsyncSession]) -> None:
    """Populate the store with the example's users, groups, roles, permissions and keys."""
    async with maker() as session:
        await _add_missing(session, User, _users())
        await _add_missing(session, Group, _groups())
        await _seed_memberships(session)
        role_ids = await _seed_roles(session)
        await _seed_permissions(session, role_ids)
        await _seed_keys(session)
        await session.commit()


async def _seed_memberships(session: AsyncSession) -> None:
    """Bind each user to its group, skipping memberships that already exist."""
    existing = await _existing_pairs(session, GroupUser, GroupUser.group_id, GroupUser.user_id)
    for user_id, group_id in MEMBERSHIPS.items():
        if (group_id, user_id) not in existing:
            session.add(GroupUser(id=uuid4(), group_id=group_id, user_id=user_id))


async def _seed_roles(session: AsyncSession) -> dict[str, UUID]:
    """Insert the roles and their group bindings; return ``role name -> role id``."""
    rows = (await session.execute(select(Role.name, Role.id))).all()
    role_ids = {name: role_id for name, role_id in rows}
    for name in ROLE_NAMES:
        if name not in role_ids:
            role_id = _derived_role_id(name)
            role_ids[name] = role_id
            session.add(Role(id=role_id, name=name))

    existing = await _existing_pairs(session, GroupRole, GroupRole.group_id, GroupRole.role_id)
    for group_id, role_name in GROUP_ROLES.items():
        if (group_id, role_ids[role_name]) not in existing:
            session.add(GroupRole(id=uuid4(), group_id=group_id, role_id=role_ids[role_name]))
    return role_ids


async def _seed_permissions(session: AsyncSession, role_ids: dict[str, UUID]) -> None:
    """Serialize :data:`ROLE_PERMISSIONS` into ``role_permissions`` rows."""
    existing = await _existing_pairs(
        session, RolePermission, RolePermission.role_id, RolePermission.resource
    )
    grants: list[tuple[str, str, Any]] = [
        (role_name, resource, policy) for (role_name, resource), policy in ROLE_PERMISSIONS.items()
    ]
    # A ``DenyAll`` on ``messages`` for the ``denied`` role, to pair with the
    # grants above and demonstrate the union model (deny does not win).
    grants.append((DENIED, "messages", DenyAll()))

    for role_name, resource, policy in grants:
        role_id = role_ids[role_name]
        if (role_id, resource) in existing:
            continue
        session.add(
            RolePermission(
                id=uuid4(),
                role_id=role_id,
                resource=resource,
                permission=policy_to_json(policy),
            )
        )


async def _seed_keys(session: AsyncSession) -> None:
    """Insert one API key per user, storing only the digest of :data:`RAW_KEYS`."""
    existing = await _existing_pairs(session, ApiKey, ApiKey.user_id)
    for user_id, raw in RAW_KEYS.items():
        if (user_id,) in existing:
            continue
        session.add(
            ApiKey(
                id=uuid4(),
                name=user_id.hex,
                key=hash_api_key(raw),
                user_id=user_id,
                roles=[],
            )
        )


async def _add_missing(session: AsyncSession, model: type[Any], rows: Sequence[Any]) -> None:
    """Add the ``rows`` whose primary key is not already stored."""
    existing = set((await session.execute(select(model.id))).scalars().all())
    for row in rows:
        if row.id not in existing:
            session.add(row)


async def _existing_pairs(
    session: AsyncSession, model: type[Any], *columns: Any
) -> set[tuple[Any, ...]]:
    """The distinct column tuples already stored for ``model``."""
    rows = (await session.execute(select(*columns).select_from(model))).all()
    return {tuple(row) for row in rows}


def _derived_role_id(name: str) -> UUID:
    """A stable role id derived from the role name, so a re-seed maps to the same row."""
    return UUID(int=ROLE_NAMES.index(name) + 1)


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
