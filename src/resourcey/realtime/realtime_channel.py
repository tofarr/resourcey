"""``Channel`` — the publish/fan-out seam a realtime publisher delivers through (issue #17).

A :class:`Channel` carries :class:`~resourcey.realtime.realtime_event.ResourceEvent`
values from the process that handled a write to every subscriber, wherever it
lives. It is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`, so a
deployment selects a concrete channel by ``kind`` (the class name) with no code
change -- exactly like every other pluggable seam (``DependencyBuilder``,
``FileStore``, ``PolicyResolver``).

Two implementations:

* :class:`InMemoryChannel` -- the single-process default and the proof the seam
  is transport-agnostic: a subscriber in the same process receives every
  published event (an in-process fan-out). It is the dev / test path.
* :class:`~resourcey.realtime.realtime_redis_channel.RedisChannel` -- the
  clustered path over ``redis.asyncio`` pub/sub, imported lazily behind the
  ``resourcey[redis]`` extra so ``realtime`` imports without the driver.

The publisher is :class:`~resourcey.triggers.redis_trigger.RedisTrigger`
despite the name of that concrete channel -- the *trigger* seam fires the
publish after a successful write
(:class:`~resourcey.triggers.triggered_service.TriggeredService`), and simply
forwards the built event to whichever ``Channel`` it was configured with;
``Channel`` itself knows nothing about triggers. The **same instance** must be
passed to the publishing trigger and to
:func:`~resourcey.realtime.realtime_routes.add_realtime` so a write in this
process reaches this process's subscribers.

A channel is its own async context manager (entered through the manifest's
``managers`` slot), so a channel that owns a client ties that client's
lifetime to the app exactly as a ``SqlSessionManager`` / ``MongoClientManager``
does.

Delivery is **at-most-once** -- pub/sub drops an event for a subscriber that is
down or briefly disconnected, and there is no replay. The reconnect reconcile
is a normal REST ``search`` (with a cursor / ``updated_at__gt=``), which the
framework already serves; the push channel is a latency optimisation, not the
source of truth.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from resourcey.realtime.realtime_event import ResourceEvent
from resourcey.util.models import DiscriminatedUnionMixin

# The per-subscriber buffer bound for the in-memory channel. Drop-oldest on
# overflow, so one slow subscriber cannot grow an unbounded server-side queue.
DEFAULT_QUEUE_MAXSIZE = 100


class Channel(DiscriminatedUnionMixin, ABC):
    """The abstract publish / subscribe seam, discriminated by ``kind``.

    A concrete channel implements :meth:`publish` and :meth:`subscribe`; both
    are async, so a channel may own a network client. It is its own async
    context manager, so the client lifecycle is tied to the app through the
    manifest's ``managers`` slot.
    """

    async def __aenter__(self) -> Channel:
        """Open the channel's client / connection (override to do real work)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the channel's client / connection (override to do real work)."""
        return None

    @abstractmethod
    async def publish(self, event: ResourceEvent) -> None:
        """Deliver ``event`` to every subscriber (at-most-once, unordered).

        Called by the publishing trigger only after the write it reports on
        has committed (a rolled-back write never reaches a trigger -- see
        ``resourcey.triggers.triggered_service``).
        """

    @abstractmethod
    def subscribe(self) -> AsyncIterator[ResourceEvent]:
        """An async iterator of events published to this channel.

        A subscriber iterates this; each subscriber gets its own iterator, so
        several WebSocket clients fan out independently. The iterator ends
        when the channel is closed.
        """


class InMemoryChannel(Channel):
    """The single-process default: an in-process fan-out of published events.

    Each :meth:`subscribe` call returns a fresh :class:`asyncio.Queue`, and
    :meth:`publish` offers the event to every live queue. The queue is
    **bounded** (:data:`DEFAULT_QUEUE_MAXSIZE`), and a full queue drops its
    **oldest** event (drop-oldest) so one slow subscriber cannot grow an
    unbounded server-side buffer -- the dropped event is the one a client would
    reconcile anyway with a REST ``search``.

    It is the dev / test path: a write in this process reaches a subscriber in
    this process with no external broker.
    """

    # The per-subscriber buffer bound. Drop-oldest on overflow.
    queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._subscribers: set[asyncio.Queue[ResourceEvent]] = set()
        self._closed = False

    async def publish(self, event: ResourceEvent) -> None:
        """Offer ``event`` to every live subscriber, dropping the oldest on a full queue."""
        for queue in list(self._subscribers):
            self._offer(queue, event)

    def subscribe(self) -> AsyncIterator[ResourceEvent]:
        """A fresh iterator over a fresh bounded queue."""
        queue: asyncio.Queue[ResourceEvent] = asyncio.Queue(maxsize=self.queue_maxsize)
        self._subscribers.add(queue)
        return self._iterate(queue)

    async def __aexit__(self, *exc: object) -> None:
        """Mark closed so every subscriber iterator ends."""
        self._closed = True
        self._subscribers.clear()

    async def _iterate(self, queue: asyncio.Queue[ResourceEvent]) -> AsyncIterator[ResourceEvent]:
        try:
            while not self._closed:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                yield event
        finally:
            self._subscribers.discard(queue)

    @staticmethod
    def _offer(queue: asyncio.Queue[ResourceEvent], event: ResourceEvent) -> None:
        """Enqueue ``event``, evicting the oldest entry when the queue is full."""
        while True:
            try:
                queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - a concurrent drain
                    return
