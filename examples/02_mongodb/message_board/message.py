"""The ``Message`` DTO + resource — the child side of the message board (MongoDB).

A message belongs to a single thread via ``thread_id`` (a UUID). Unlike the SQL
example (which uses a real foreign-key column), the Mongo variant stores
``thread_id`` as a plain scalar field — MongoDB has no server-side FK
constraints, so the relation is enforced at the application level. ``v2``'s
projection is plain-columns-only (relationship / nested projection is a known
limitation), which is exactly what this example needs.

The example opts into a **declared** filter surface: a
:class:`~resourcey.v2.util.search_filter.BaseObjectFilter` returned from
``get_search_filter_type``. Its ``<attribute>__<op>`` fields are the whole
surface, so ``GET /messages?thread_id__eq=<id>`` lists a thread's messages and
``?text__contains=`` does a substring search on the body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from resourcey.v2.core.dto import DTO
from resourcey.v2.mongo.mongo_resource import MongoResource
from resourcey.v2.util.search_filter import BaseObjectFilter


class MessageDTO(DTO):
    """A message belonging to a thread.

    Fields:
        id: Client-supplied UUID primary key (stored under Mongo's ``_id``).
        thread_id: UUID of the parent thread (required, application-level FK).
        text: The message body (required).
        created_at: Framework-owned; set once on create.
        updated_at: Framework-owned; refreshed on every update.
    """

    id: UUID
    thread_id: UUID
    text: str
    created_at: datetime
    updated_at: datetime


class MessageSearchFilter(BaseObjectFilter[MessageDTO]):
    """Optional filter clauses for ``Message`` search.

    ``thread_id__eq`` lists a thread's messages; ``text__contains`` does a
    substring search on the body.
    """

    thread_id__eq: UUID | None = None
    text__contains: str | None = None


class MessageResource(MongoResource[MessageDTO, UUID]):
    """``MessageDTO`` served from the ``messages`` collection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("path", "messages")
        kwargs.setdefault("collection_name", "messages")
        super().__init__(*args, **kwargs)

    def get_search_filter_type(self) -> type[MessageSearchFilter]:
        """Expose ``thread_id__eq`` / ``text__contains`` and nothing else."""
        return MessageSearchFilter
