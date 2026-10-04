"""``Trigger`` — the edit-event callback contract (issue #155).

A :class:`Trigger` is a configured, polymorphic callback: a
:class:`~resourcey.util.models.DiscriminatedUnionMixin`, mirroring
:class:`~resourcey.tasks.task.BackgroundTask`'s shape. An app declares its own
trigger kinds (a webhook notifier, a pub/sub publisher, …) as subclasses
carrying whatever pydantic config fields they need — no framework schema
change, and each kind is env-parseable by dotted path
(``APP_TRIGGERS_<n>_TRIGGER_KIND=myapp.webhooks.NotifyWebhook``, the existing
env-parser support for a ``kind``-discriminated field)::

    class NotifyWebhook(Trigger[T, K]):
        url: str

        async def callback(self, edits, results) -> None:
            async with httpx.AsyncClient() as client:
                await client.post(self.url, json=...)

:class:`Trigger` is generic over the DTO type ``T`` and the identifier type
``K``, the same two parameters
:class:`~resourcey.core.service.Service` carries. A concrete trigger kind
stays **open** over both (``class NotifyWebhook(Trigger[T, K])``, never
``Trigger[SomeDto, int]``) — the same "leaf stays generic" shape
:class:`~resourcey.util.search_filter.AllFilter` uses — because one configured
trigger instance may be attached to resources serving different DTOs
(:meth:`~resourcey.triggers.trigger_config.TriggerConfig.resource_triggers`
groups by resource path, not by type), so it cannot bind ``T`` / ``K`` to one
backend's types. :class:`~resourcey.triggers.triggered_service.TriggeredService`
/ :class:`~resourcey.triggers.triggered_resource.TriggeredResource` are
parameterized by the resource they wrap, so ``edits`` / ``results`` are typed
precisely at the call site even though the trigger itself stays open.

:meth:`callback` is invoked **once per edit operation** (never per item): a
single ``create()`` normalizes to ``[Create(item=payload)]`` /
``[result]``; a ``batch_edit`` of five passes its five :class:`Edit` nodes and
their five results verbatim. ``results`` is the inner service's return,
positionally aligned with ``edits`` — a delete or a miss is ``None``, and a
miss still fires (it is not an error). The trigger receives the **inner DTO**
instances, not the projected REST models: serialization is the trigger's own
job.

A trigger is config, not a bare callable: ``on_edit`` on
:class:`~resourcey.triggers.triggered_resource.TriggeredResource` accepts only
``Trigger`` instances, so a plain function (which would carry no config
fields) is rejected at construction.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable

from resourcey.core.service import Create, Delete, Update
from resourcey.util.models import DiscriminatedUnionMixin

if TYPE_CHECKING:
    from resourcey.core.resource import Resource

T = TypeVar("T")
K = TypeVar("K")

# One operation's edits (normalized to a list, never per-item) and the inner
# service's positionally aligned results, generic over the DTO type ``T`` and
# the identifier type ``K`` so a concrete resource's trigger call site is typed
# precisely (``TriggerEdits[ThreadDto, int]``); a trigger kind that stays open
# over both (see the module docstring) uses them unparameterized.
TriggerEdits = list[Create[T] | Update[T] | Delete[K]]
TriggerResults = list[T | None]


class Trigger(DiscriminatedUnionMixin, ABC, Generic[T, K]):
    """A configured callback fired after a successful edit operation.

    Generic over the DTO type ``T`` and the identifier type ``K`` — the same
    two parameters :class:`~resourcey.core.service.Service` carries — so
    :meth:`callback`'s ``edits`` / ``results`` are typed precisely wherever a
    resource's concrete types are known, while a concrete trigger kind stays
    open over both (see the module docstring) since one configured instance
    may serve resources with different DTOs.

    Subclasses declare whatever pydantic fields their delivery channel needs
    (a webhook's ``url``, a pub/sub publisher's ``channel``, …) and implement
    :meth:`callback`. The base is abstract, so only concrete kinds are
    instantiable — the same shape as
    :class:`~resourcey.tasks.task.BackgroundTask`.
    """

    @abstractmethod
    async def callback(self, edits: TriggerEdits[T, K], results: TriggerResults[T]) -> None:
        """Run after a successful edit operation.

        Called **only on success** (an inner exception fires no trigger) and
        **once per operation**, not per item. A raising callback never fails
        the request: the caller
        (:class:`~resourcey.triggers.triggered_service.TriggeredService`)
        isolates each trigger's invocation in its own catch and logs it, so
        one trigger raising never stops the rest from running.
        """


@runtime_checkable
class ResourceBoundTrigger(Protocol):
    """Optional protocol: a trigger that wants the resource it is attached to.

    The base :class:`Trigger` contract does not require this -- a generic
    trigger (e.g. :class:`~resourcey.triggers.webhook_trigger.WebhookTrigger`)
    needs no resource context. :class:`~resourcey.triggers.redis_trigger.RedisTrigger`
    is the first consumer: it needs the resource's path and read-model type to
    tag and project a row before publishing, so a field a
    :class:`~resourcey.view.resource_view.ResourceView` hides from the REST
    read surface does not leak onto the realtime channel either.

    :class:`~resourcey.triggers.triggered_resource.TriggeredResource` and
    :class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`
    call :meth:`bind_resource` once -- at construction / registration time, not
    per request -- for any configured trigger that implements it (checked with
    ``isinstance(trigger, ResourceBoundTrigger)``, which ``@runtime_checkable``
    makes a plain method-presence test).
    """

    def bind_resource(self, resource: Resource[Any, Any]) -> None:
        """Receive the resource this trigger is attached to."""
