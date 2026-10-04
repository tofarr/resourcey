"""``RedisTrigger`` — publishes edit events to a realtime channel (issues #17 / #155).

The predecessor to this module (an unmerged PR against issue #17) published
realtime events through a bespoke ``NotifyingService`` wrapping *every*
resource's service, before :mod:`resourcey.triggers` (issue #155) existed.
This module is the re-expression on top of the trigger seam instead: a
:class:`~resourcey.triggers.trigger.Trigger` subclass, attached the same way
:class:`~resourcey.triggers.webhook_trigger.WebhookTrigger` is (``on_edit=
[RedisTrigger(...)]`` on a :class:`~resourcey.triggers.triggered_resource.TriggeredResource`,
or via the env-driven :class:`~resourcey.triggers.trigger_config.TriggerConfig`
rung) -- no new resource wrapper, no change to every resource's service, and
the generic ``Trigger`` execution policy (success-only, once-per-operation,
per-trigger isolation, background-by-default) applies for free.

Despite the name, delivery itself is **not** hard-wired to Redis: a
``RedisTrigger`` forwards each event to a configured
:class:`~resourcey.realtime.realtime_channel.Channel` (default
:class:`~resourcey.realtime.realtime_channel.InMemoryChannel`, so constructing
one adds no new dependency). The ``channel=`` is named for the production case
this trigger exists for -- a
:class:`~resourcey.realtime.realtime_redis_channel.RedisChannel`, so a write in
one app process reaches a WebSocket subscriber
(:func:`~resourcey.realtime.realtime_routes.add_realtime`) connected to
another. The **same channel instance** must be passed to both the trigger and
``add_realtime``, and entered through ``Manifest(managers=[channel])`` so its
client lifecycle is tied to the app.

Field-level safety
-------------------
:meth:`bind_resource` (the optional
:class:`~resourcey.triggers.trigger.ResourceBoundTrigger` protocol, called
automatically by ``TriggeredResource`` / ``TriggeredDependencyBuilder`` at
construction / registration time) gives this trigger the resource it is
attached to, so the published event's ``item`` is the **read-model
projection** (``get_rest_models().read_response``), never the raw DTO -- a
field a :class:`~resourcey.view.resource_view.ResourceView` hides from the
REST read surface must not leak onto the realtime channel either. A
``RedisTrigger`` constructed and used directly, without that binding (e.g. in
isolation in a test), falls back to the DTO's own fields and assumes the
conventional ``id`` identifier -- a reduced-safety path, logged nowhere but
documented here.

Vocabulary: one :class:`~resourcey.realtime.realtime_event.ResourceEvent` per
``(edit, result)`` pair, **not** one per :meth:`callback` invocation -- a
``batch_edit`` of five publishes five discrete events, matching what a
WebSocket subscriber expects to filter and deliver one row at a time. A
``Delete`` always publishes (``item=None``, there is no row left); a
``Create`` / ``Update`` with a ``None`` result (a ``batch_edit`` miss)
publishes nothing -- there is no row to report.

This module is part of ``resourcey.triggers``; it imports
``resourcey.realtime`` (the event / channel vocabulary this trigger builds on)
-- both sit at the same layer rank, and ``realtime`` does not import
``triggers`` back, so no cycle exists.
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

# The default identifier field name assumed when this trigger is used without
# bind_resource -- the conventional id. A resource with a custom
# id_field_name should rely on bind_resource (automatic via
# TriggeredResource / TriggeredDependencyBuilder) instead.
_DEFAULT_ID_FIELD = "id"


def _dto_values(instance: BaseModel) -> dict[str, Any]:
    """A DTO instance's fields, dropping those left ``MISSING`` (unset).

    Mirrors ``http.routes._project``: a value a service never populated must
    not be forced through a narrower model's validation.
    """
    return {name: value for name, value in vars(instance).items() if value is not MISSING}


class RedisTrigger(Trigger[Any, Any]):
    """Publishes a :class:`ResourceEvent` per edit to a :class:`Channel`.

    Attributes:
        channel: The channel events are forwarded to. Defaults to an
            unshared :class:`InMemoryChannel` (no new dependency); pass a
            shared :class:`~resourcey.realtime.realtime_redis_channel.RedisChannel`
            instance (also entered via ``Manifest(managers=[...])`` and also
            passed to :func:`~resourcey.realtime.realtime_routes.add_realtime`)
            to bridge processes.
        resource_path: An explicit override for the event's ``resource`` tag
            (the WebSocket subscription key). When unset, resolved from the
            bound resource's :meth:`~resourcey.core.resource.Resource.get_resource_path`
            -- set this only when publishing under a different logical name.
    """

    channel: Channel = Field(default_factory=InMemoryChannel)
    resource_path: str | None = None

    _resource: Any = PrivateAttr(default=None)

    def bind_resource(self, resource: Resource[Any, Any]) -> None:
        """Record the resource this trigger is attached to (see the module docstring)."""
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
    expose -- see the module docstring's "field-level safety" section.
    """
    if read_response is None:
        return result.model_dump(mode="json")
    projected = read_response.model_validate(_dto_values(result))
    return projected.model_dump(mode="json")
