"""The ``Message`` resource — the child side of the message board.

``v2`` is model-first: ``Message`` the ORM model (in :mod:`message_board.models`)
is the schema of record, and :class:`~resourcey.v2.sql.sql_resource.SqlResource`
infers the DTO and the REST models from it.

The example opts into a **declared** filter surface: a
:class:`~resourcey.v2.util.search_filter.BaseObjectFilter` returned from
``get_search_filter_type``. Its ``<attribute>__<op>`` fields are the whole
surface, so ``GET /messages?thread_id__eq=<id>`` lists a thread's messages, and
``?text__contains=`` does a substring search on the body. Without it, ``v2``
would derive a wider surface from the read model (every readable field with its
type's operators).
"""

from __future__ import annotations

from typing import Any

from message_board.models import Message
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.util.search_filter import BaseObjectFilter


class MessageSearchFilter(BaseObjectFilter[Message]):
    """Optional filter clauses for ``Message.search``.

    ``thread_id__eq`` lists a thread's messages; ``text__contains`` does a
    substring search on the body.
    """

    thread_id__eq: int | None = None
    text__contains: str | None = None


class MessageResource(SqlResource[Any, Any]):
    """The ``Message`` ORM model exposed with the declared search filter above."""

    def get_search_filter_type(self) -> type[MessageSearchFilter]:
        """Expose ``thread_id__eq`` / ``text__contains`` and nothing else."""
        return MessageSearchFilter
