"""``ResourceEvent`` — the change vocabulary a resource emits (issue #17).

An event says *what changed* independently of how it is carried: the resource,
the kind of change (``created`` / ``updated`` / ``deleted``), the row's
identifier, a monotonic timestamp, and — for the write actions that leave a row
behind — the **read-model projection** of that row.

Two deliberate choices:

* **The payload is the read-model projection, never the DTO / storage row.** A
  field hidden by a :class:`~resourcey.view.resource_view.ResourceView` must not
  leak through the event stream any more than through a REST body, so the event
  reuses the one security gate the REST layer already enforces
  (``get_rest_models().read_response``). The client also decodes exactly the
  shape it already knows from ``search`` / ``read``.
* **The vocabulary is write-only.** ``created`` / ``updated`` / ``deleted`` are
  emitted; a ``read`` emits nothing (a read event would fire on the hottest path,
  duplicate data the caller already has, and let an unauthenticated reader drive
  fan-out — the reconnect reconcile is a normal REST ``search``). A resource
  emits nothing for an action it does not declare, so a read-only
  :class:`~resourcey.list.list_resource.ListResource` emits nothing by
  construction.

The event is a plain frozen Pydantic model: it is serialized to JSON for the
Redis bridge and to the WebSocket wire, so it must be a self-contained value.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """The change a resource emitted — the write actions, and nothing else.

    A member's value is the wire string (``"created"``), and the singular
    :class:`~resourcey.core.service.Action` it corresponds to has the same value
    (``Action.CREATE == "create"``), so the mapping is explicit in
    :func:`event_kind_for_action` rather than string-munged.
    """

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class ResourceEvent(BaseModel):
    """One resource change, as delivered to a subscriber.

    Attributes:
        resource: The resource's REST path (``threads``), which is also the
            subscription key.
        kind: Whether the row was created, updated, or deleted.
        id: The changed row's identifier (as its wire form).
        timestamp: When the change was observed (UTC).
        item: The row's read-model projection for a ``created`` / ``updated``
            event, or ``None`` for a ``deleted`` event (there is no row left to
            project). The projection, never the internal DTO / storage row, so a
            hidden field cannot leak.
    """

    model_config = ConfigDict(frozen=True)

    resource: str
    kind: EventKind
    id: Any
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    item: dict[str, Any] | None = None


# The singular action -> event kind mapping. ``read`` and its derived actions
# are deliberately absent: a read emits nothing.
_EVENT_KIND_FOR_ACTION: dict[str, EventKind] = {
    "create": EventKind.CREATED,
    "update": EventKind.UPDATED,
    "delete": EventKind.DELETED,
}


def event_kind_for_action(action: Any) -> EventKind | None:
    """The event kind a successful ``action`` emits, or ``None`` if it emits nothing.

    ``create`` -> ``created``, ``update`` -> ``updated``, ``delete`` ->
    ``deleted``; every read-like action (and ``batch_edit``, which fans out into
    its own per-edit events) returns ``None``.
    """
    return _EVENT_KIND_FOR_ACTION.get(str(action))
