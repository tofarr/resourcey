"""The ``Message`` resource — the owner-scoped side of the board.

The ORM model (in :mod:`simple_roles.models`) is the schema of record, so
:class:`~resourcey.v2.sql.sql_resource.SqlResource` infers the DTO and the REST
models from it.

``author_id`` is not a client field: its column carries a
:class:`~resourcey.v2.core.dto.DtoField` (in the column's ``info``, the
documented escape hatch) with ``in_create_request=False`` /
``in_update_request=False``. A caller therefore cannot choose or rewrite a row's
owner; :class:`MessageService` stamps it on create from the authenticated
principal the dependency builder published on the call-scoped ``ctx``. The
``Owner`` policy leaves ``create`` unscoped (a new row has no owner yet), so this
is where the new row acquires one.
"""

from __future__ import annotations

from typing import Any

from resourcey.v2.auth.auth_principal import PRINCIPAL_CTX_KEY
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.sql.sql_service import SqlService
from resourcey.v2.util.missing import MISSING


class MessageService(SqlService[Any, Any]):
    """A ``Message`` service that stamps the owner on create from the principal."""

    async def create(self, payload: Any) -> Any:
        principal = self._ctx.get(PRINCIPAL_CTX_KEY)
        author = getattr(payload, "author_id", MISSING)
        if principal is not None and author in (MISSING, None):
            payload = payload.model_copy(update={"author_id": principal.id})
        return await super().create(payload)


class MessageResource(SqlResource[Any, Any]):
    """The ``Message`` model exposed with server-side owner stamping."""

    def make_service(self, ctx: Any, session_factory: Any) -> Any:
        return MessageService(self, ctx, session_factory)
