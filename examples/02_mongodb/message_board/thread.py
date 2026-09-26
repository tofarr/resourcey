"""The ``Thread`` DTO + resource — the parent side of the message board (MongoDB).

``v2``'s Mongo workflow is **DTO-first** (Mongo has no schema of record): the
developer declares a :class:`~resourcey.v2.core.dto.DTO` and
:class:`~resourcey.v2.mongo.mongo_resource.MongoResource` serves it. The DTO
declaration drives the six REST models, the identifier, and the query surface
(the read model *is* the filter / sort surface).

Unlike the SQL example (01), the identifier is a client-supplied ``UUID`` —
MongoDB has no auto-increment — and there is no migration step: the schema is
created implicitly on first write.

The ``v2/core`` DTO conventions supply what the ``v1`` example hand-wrote: the
bare ``id: UUID`` gets a ``uuid4`` create default, ``created_at`` is set once on
create, and ``updated_at`` is refreshed on every update — none of them appear in
a request shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field

from resourcey.v2.core.dto import DTO
from resourcey.v2.mongo.mongo_resource import MongoResource


class ThreadDTO(DTO):
    """A message-board thread.

    Fields:
        id: Client-supplied UUID primary key (stored under Mongo's ``_id``).
        title: Short, human-readable title (required, 1-200 chars).
        description: Longer optional body text.
        created_at: Framework-owned; set once on create.
        updated_at: Framework-owned; refreshed on every update.
    """

    id: UUID
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = ""
    created_at: datetime
    updated_at: datetime


class ThreadResource(MongoResource[ThreadDTO, UUID]):
    """``ThreadDTO`` served from the ``threads`` collection.

    The path is given explicitly because the default derives from the DTO class
    name (``thread-dtos``); the example keeps the clean ``/threads`` surface.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("path", "threads")
        kwargs.setdefault("collection_name", "threads")
        super().__init__(*args, **kwargs)
