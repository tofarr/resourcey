"""``RedisTrigger`` — publishes edit events to a realtime channel (issues #17 / #155).

A ``Trigger`` subclass, attached the same way ``WebhookTrigger`` is, so the
generic execution policy (success-only, once-per-operation, per-trigger
isolation, background-by-default) applies for free.

Despite the name, delivery itself is **not** hard-wired to Redis: a
``RedisTrigger`` forwards each event to a configured ``Channel`` (default
``InMemoryChannel``, so constructing one adds no new dependency). ``channel=`` is
named for the production case this trigger exists for — a ``RedisChannel``, so a
write in one app process reaches a WebSocket subscriber on another. The **same
channel instance** must be passed to both the trigger and ``add_realtime``, and
entered through ``Manifest(managers=[channel])``.

Field-level safety: ``bind_resource`` (called automatically by the wrappers)
gives this trigger the resource it is attached to, so the published ``item`` is
the **read-model projection**, never the raw DTO — a field a ``ResourceView``
hides from REST must not leak onto the realtime channel either. A trigger used
without that binding falls back to the DTO's own fields and assumes the
conventional ``id`` identifier.

Vocabulary: one ``ResourceEvent`` per ``(edit, result)`` pair, **not** one per
``callback`` invocation. A ``Delete`` always publishes (``item=None``); a
``Create`` / ``Update`` with a ``None`` result publishes nothing.

This module is part of ``resourcey.triggers``; it imports
``resourcey.realtime``, which does not import ``triggers`` back, so no cycle
exists.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from pydantic_core import to_jsonable_python

from resourcey.core.resource import Resource
from resourcey.core.service import Create, Delete, Update
from resourcey.realtime.realtime_channel import Channel, InMemoryChannel
from resourcey.realtime.realtime_event import EventKind, ResourceEvent
from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults
from resourcey.util.missing import MISSING

# The identifier assumed when used without bind_resource; a custom
# id_field_name should rely on bind_resource.
_DEFAULT_ID_FIELD = "id"


def _dto_values(instance: BaseModel) -> dict[str, Any]:
    """A DTO instance's fields, dropping those left ``MISSING`` (unset)."""
    return {name: value for name, value in vars(instance).items() if value is not MISSING}


class RedisTrigger(Trigger[Any, Any]):
    """Publishes a ``ResourceEvent`` per edit to a ``Channel``.

    ``resource_path`` overrides the event's ``resource`` tag (the WebSocket
    subscription key); when unset it is resolved from the bound resource.
    """

    channel: Channel = Field(default_factory=InMemoryChannel)
    resource_path: str | None = None

    _resource: Any = PrivateAttr(default=None)

    def bind_resource(self, resource: Resource[Any, Any]) -> None:
        """Record the resource this trigger is attached to."""
        self._resource = resource

    async def callback(self, edits: TriggerEdits[Any, Any], results: TriggerResults[Any]) -> None:
        resource_path = self._resolve_resource_path()
        id_field = (
            self._resource.get_id_field() if self._resource is not None else _DEFAULT_ID_FIELD
        )
        read_response = (
            self._resource.get_rest_models().read_response if self._resource is not None else None
        )
        for edit, result in zip(edits, results, strict=True):
            event = self._build_event(edit, result, resource_path, id_field, read_response)
            if event is not None:
                await self.channel.publish(event)

    def _resolve_resource_path(self) -> str:
        if self.resource_path is not None:
            return self.resource_path
        if self._resource is not None:
            return str(self._resource.get_resource_path())
        raise ValueError(
            "RedisTrigger has no resource path: pass resource_path= explicitly, or "
            "attach it via TriggeredResource(..., on_edit=[...]) / "
            "TriggeredDependencyBuilder so bind_resource() runs automatically."
        )

    def _build_event(
        self,
        edit: Create[Any] | Update[Any] | Delete[Any],
        result: Any,
        resource_path: str,
        id_field: str,
        read_response: type[BaseModel] | None,
    ) -> ResourceEvent | None:
        """One ``ResourceEvent`` for ``(edit, result)``, or ``None`` for a miss."""
        if isinstance(edit, Delete):
            return ResourceEvent(
                resource=resource_path,
                kind=EventKind.DELETED,
                id=to_jsonable_python(edit.id),
            )
        if result is None:
            # A batch_edit miss (an absent id) -- nothing happened, nothing to report.
            return None
        item = _project_item(result, read_response)
        row_id = item.get(id_field, to_jsonable_python(getattr(result, id_field, None)))
        kind = EventKind.CREATED if isinstance(edit, Create) else EventKind.UPDATED
        return ResourceEvent(resource=resource_path, kind=kind, id=row_id, item=item)


def _project_item(result: BaseModel, read_response: type[BaseModel] | None) -> dict[str, Any]:
    """Project ``result`` onto ``read_response`` when bound, else dump it verbatim.

    The projected path drops a field the bound resource's read model does not
    expose.
    """
    if read_response is None:
        return result.model_dump(mode="json")
    projected = read_response.model_validate(_dto_values(result))
    return projected.model_dump(mode="json")
