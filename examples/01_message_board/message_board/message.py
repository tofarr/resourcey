"""The ``Message`` resource — the child side of the message board.

The framework is model-first: ``Message`` the ORM model (in :mod:`message_board.models`)
is the schema of record, and :class:`~resourcey.sql.sql_resource.SqlResource`
infers the DTO and the REST models from it.

The query surface is the read model: a field is filterable / sortable exactly
when the read model exposes it, so ``GET /messages?thread_id__eq=<id>`` lists a
thread's messages and ``?text__contains=`` does a substring search on the body.
"""

from __future__ import annotations

from typing import Any

from resourcey.sql.sql_resource import SqlResource


class MessageResource(SqlResource[Any, Any]):
    """The ``Message`` ORM model exposed with the derived query surface."""
