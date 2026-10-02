"""Seed the example's local user store and external-identity map.

The identity provider owns the *external* subjects; this example owns the
*internal* users they resolve to. Each ``ExternalIdentity`` row links a provider
``(issuer, subject)`` pair to a local ``user_id``, and the ``OAuthAuthenticator``
resolves that pair per request — a first-seen pair with no mapping is
fail-closed.

The committed Alembic migration inserts these rows, so ``alembic upgrade head``
yields a database whose users and links match the dev tokens; this module holds
the fixed ids both the migration and the tests use, plus :func:`seed` for a
database built directly from ``Base.metadata`` (the smoke tests, an embedded
deployment).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oauth_example.dev_idp import DEV_ISSUER
from oauth_example.models import User
from resourcey.auth.auth_oauth_client import ExternalIdentity

# Fixed ids so a dev token's subject resolves to a known local user.
ADMIN_ID = UUID("00000000-0000-0000-0000-0000000000a0")
USER_ID = UUID("00000000-0000-0000-0000-0000000000b0")

# The external subjects the dev tokens carry, mapped to the local users above.
ADMIN_SUBJECT = "dev-admin"
USER_SUBJECT = "dev-user"

SEED_USERS: tuple[dict[str, Any], ...] = (
    {"id": ADMIN_ID, "email": "admin@example.com", "username": "admin", "enabled": True},
    {"id": USER_ID, "email": "user@example.com", "username": "user", "enabled": True},
)

SEED_IDENTITIES: tuple[dict[str, Any], ...] = (
    {"issuer": DEV_ISSUER, "subject": ADMIN_SUBJECT, "user_id": ADMIN_ID},
    {"issuer": DEV_ISSUER, "subject": USER_SUBJECT, "user_id": USER_ID},
)


async def seed(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Insert the fixed users and identity links, leaving existing rows untouched.

    Idempotent: re-running against a seeded database adds nothing, so it is safe
    to call from a test fixture or a one-off script.
    """
    async with session_factory() as session:
        for spec in SEED_USERS:
            if await session.get(User, spec["id"]) is None:
                session.add(User(**spec))
        for spec in SEED_IDENTITIES:
            identity_id = _identity_id(spec["issuer"], spec["subject"])
            if await session.get(ExternalIdentity, identity_id) is None:
                session.add(ExternalIdentity(id=identity_id, **spec))
        await session.commit()


def _identity_id(issuer: str, subject: str) -> UUID:
    """A deterministic id for a ``(issuer, subject)`` link (so seeding is idempotent)."""
    import hashlib

    digest = hashlib.sha256(f"{issuer}\x00{subject}".encode()).digest()
    return UUID(bytes=digest[:16])
