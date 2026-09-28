"""Seed the example's stored principals.

The accepted API keys live in the environment (``APP_API_KEYS_*``), but the
**principals** those keys act as are rows in the ``users`` table. Each key whose
``PRINCIPAL_ID`` names a user resolves to that stored principal, and the
authenticator rejects a key whose principal is missing or ``enabled=False``.

The committed Alembic migration inserts these two rows, so ``alembic upgrade
head`` yields a database whose users match the ``.env`` keys. This module holds
the fixed ids both the migration and the tests use, plus :func:`seed_users` for
a database built directly from ``Base.metadata`` (the smoke tests, an embedded
deployment).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from simple_roles.models import User

# Fixed ids so the ``APP_API_KEYS_<n>_PRINCIPAL_ID`` values in ``.env`` can name
# them. The ``admin`` key acts as ``ADMIN_ID``; the ``user`` key acts as
# ``USER_ID`` (the "own rows only" principal the ``Owner`` policy scopes on).
ADMIN_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("11111111-1111-1111-1111-111111111111")

SEED_USERS: tuple[dict[str, Any], ...] = (
    {
        "id": ADMIN_ID,
        "email": "admin@example.com",
        "username": "admin",
        "enabled": True,
    },
    {
        "id": USER_ID,
        "email": "user@example.com",
        "username": "user",
        "enabled": True,
    },
)


async def seed_users(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert the fixed seed principals, leaving an existing row untouched.

    Idempotent: re-running against a seeded database adds nothing, so it is safe
    to call from a test fixture or a one-off script.
    """
    async with session_factory() as session:
        for spec in SEED_USERS:
            if await session.get(User, spec["id"]) is None:
                session.add(User(**spec))
        await session.commit()
