"""The lookup services behind the OAuth resources (issue #151).

Both client resources expose the same seam the authenticator validates through::

    async def find_by_issuer(self, issuer: str) -> Any | None

``issuer`` is the presented token's ``iss`` claim; a match is the client row /
config entry whose ``issuer`` equals it. The lookup is a dedicated method, never
a :class:`~resourcey.util.search_filter.SearchFilter` — the public query surface
is closed over the client secret, so a guess-confirmation oracle over it must not
be reachable.

:class:`ExternalIdentityService` adds the ``(issuer, subject)`` mapping lookup
that correlates an external principal across providers, plus the link helper.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from resourcey.list.list_service import ListService
from resourcey.sql.sql_service import SqlService


class OAuthClientService(SqlService[Any, Any]):
    """The DB-backed client service, with ``find_by_issuer``."""

    async def find_by_issuer(self, issuer: str) -> Any | None:
        """The stored client row whose ``issuer`` equals ``issuer``, or ``None``.

        A direct indexed equality lookup through the resource's escape hatch,
        never the public query surface.
        """
        session = self._active_session()
        column = self._resource.table.c["issuer"]
        statement = self._resource.table.select().where(column == issuer).limit(1)
        row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        return self._to_dto(row)


class ConfigOAuthClientService(ListService[Any, Any]):
    """The read-only config client service, with ``find_by_issuer``."""

    async def find_by_issuer(self, issuer: str) -> Any | None:
        """The config entry whose ``issuer`` equals ``issuer``, or ``None``."""
        self._require_entered()
        for item in self._items:
            if getattr(item, "issuer", None) == issuer:
                return item
        return None


class ExternalIdentityService(SqlService[Any, Any]):
    """The ``(issuer, subject)`` -> ``user_id`` mapping, with a lookup + link seam."""

    async def find_by_issuer_subject(self, issuer: str, subject: str) -> Any | None:
        """The stored mapping for ``(issuer, subject)``, or ``None``.

        The pair is unique, so at most one row matches. A miss is the
        fail-closed default (a first-seen external principal is rejected unless
        an app opts into auto-provisioning).
        """
        session = self._active_session()
        table = self._resource.table
        statement = (
            table.select().where(table.c.issuer == issuer, table.c.subject == subject).limit(1)
        )
        row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        return self._to_dto(row)

    async def link(self, issuer: str, subject: str, user_id: UUID) -> UUID:
        """Link ``(issuer, subject)`` to ``user_id`` (idempotent); return the user id."""
        existing = await self.find_by_issuer_subject(issuer, subject)
        if existing is not None:
            return UUID(str(existing.user_id))
        session = self._active_session()
        table = self._resource.table
        await session.execute(
            table.insert().values(id=uuid4(), issuer=issuer, subject=subject, user_id=user_id)
        )
        return user_id
