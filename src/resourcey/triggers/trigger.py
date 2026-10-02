"""``Trigger`` — the edit-event callback contract (issue #155).

A :class:`Trigger` is a configured, polymorphic callback: a
:class:`~resourcey.util.models.DiscriminatedUnionMixin`, mirroring
:class:`~resourcey.tasks.task.BackgroundTask`'s shape. An app declares its own
trigger kinds (a webhook notifier, a pub/sub publisher, …) as subclasses
carrying whatever pydantic config fields they need — no framework schema
change, and each kind is env-parseable by dotted path
(``APP_TRIGGERS_<n>_TRIGGER_KIND=myapp.webhooks.NotifyWebhook``, the existing
env-parser support for a ``kind``-discriminated field)::

    class NotifyWebhook(Trigger):
        url: str

        async def callback(self, edits, results) -> None:
            async with httpx.AsyncClient() as client:
                await client.post(self.url, json=...)

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
from typing import Any

from resourcey.core.service import Create, Delete, Update
from resourcey.util.models import DiscriminatedUnionMixin

# One operation's edits (normalized to a list, never per-item) and the inner
# service's positionally aligned results. Typed loosely (``Any`` for the DTO /
# id types): a trigger is configured once and may be attached to resources
# serving different DTOs, so it cannot be bound to one backend's ``T`` / ``K``.
TriggerEdits = list[Create[Any] | Update[Any] | Delete[Any]]
TriggerResults = list[Any | None]


class Trigger(DiscriminatedUnionMixin, ABC):
    """A configured callback fired after a successful edit operation.

    Subclasses declare whatever pydantic fields their delivery channel needs
    (a webhook's ``url``, a pub/sub publisher's ``channel``, …) and implement
    :meth:`callback`. The base is abstract, so only concrete kinds are
    instantiable — the same shape as
    :class:`~resourcey.tasks.task.BackgroundTask`.
    """

    @abstractmethod
    async def callback(self, edits: TriggerEdits, results: TriggerResults) -> None:
        """Run after a successful edit operation.

        Called **only on success** (an inner exception fires no trigger) and
        **once per operation**, not per item. A raising callback never fails
        the request: the caller
        (:class:`~resourcey.triggers.triggered_service.TriggeredService`)
        isolates each trigger's invocation in its own catch and logs it, so
        one trigger raising never stops the rest from running.
        """
