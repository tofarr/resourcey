"""``ResourceEvent`` — the change vocabulary published over a realtime channel (issue #17).

An event says *what changed* independently of how it is carried: the resource,
the kind of change, the row's identifier, a timestamp, and — for the write actions
that leave a row behind — the row's projection.

Two deliberate choices:

* **The payload is the read-model projection, never the raw DTO**, whenever the
  publishing trigger knows its resource. A field hidden by a ``ResourceView`` must
  not leak through the event stream any more than through a REST body.
* **The vocabulary is write-only.** A ``read`` emits nothing. The reconnect
  reconcile is a normal REST ``search``.

The event is a plain frozen Pydantic model: it is serialized to JSON for the
Redis bridge and the WebSocket wire.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """The change a resource emitted -- the write actions, and nothing else."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class ResourceEvent(BaseModel):
    """One resource change, as delivered to a subscriber.

    ``resource`` is the REST path (also the subscription key). ``item`` is
    ``None`` for a ``deleted`` event, since there is no row left to project.
    """

    model_config = ConfigDict(frozen=True)

    resource: str
    kind: EventKind
    id: Any
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    item: dict[str, Any] | None = None
