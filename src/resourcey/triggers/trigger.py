"""``Trigger`` — the edit-event callback contract (issue #155).

A :class:`Trigger` is a configured, polymorphic callback, mirroring
``BackgroundTask``'s shape. An app declares its own trigger kinds (a webhook
notifier, a pub/sub publisher, …) as subclasses carrying whatever pydantic config
fields they need — no framework schema change, and each kind is env-parseable by
dotted path::

    class NotifyWebhook(Trigger[T, K]):
        url: str

        async def callback(self, edits, results) -> None:
            ...

:class:`Trigger` is generic over the DTO type ``T`` and the identifier type
``K``. A concrete trigger kind stays **open** over both, because one configured
trigger instance may be attached to resources serving different DTOs.

:meth:`callback` is invoked **once per edit operation** (never per item): a
single ``create()`` normalizes to ``[Create(item=payload)]`` / ``[result]``; a
batch of five edits passes its five ``Edit`` nodes and their five results
verbatim. ``results`` is positionally aligned with ``edits`` — a delete or a miss
is ``None``, and a miss still fires. The trigger receives the **inner DTO**
instances, not the projected REST models.

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

# One operation's edits (never per-item) and its aligned results.
TriggerEdits = list[Create[T] | Update[T] | Delete[K]]
TriggerResults = list[T | None]


class Trigger(DiscriminatedUnionMixin, ABC, Generic[T, K]):
    """A configured callback fired after a successful edit operation.

    Subclasses declare whatever pydantic fields their delivery channel needs
    and implement :meth:`callback`. The base is abstract, so only concrete
    kinds are instantiable.
    """

    @abstractmethod
    async def callback(self, edits: TriggerEdits[T, K], results: TriggerResults[T]) -> None:
        """Run after a successful edit operation.

        Called **only on success** and **once per operation**, not per item. A
        raising callback never fails the request: the caller isolates each
        trigger's invocation and logs it.
        """


@runtime_checkable
class ResourceBoundTrigger(Protocol):
    """Optional protocol: a trigger that wants the resource it is attached to.

    A generic trigger needs no resource context; ``RedisTrigger`` does, to tag
    and project a row before publishing. The wrappers call
    :meth:`bind_resource` once, at registration time, for any trigger that
    implements it.
    """

    def bind_resource(self, resource: Resource[Any, Any]) -> None:
        """Receive the resource this trigger is attached to."""
