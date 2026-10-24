"""``Channel`` — the publish/fan-out seam a realtime publisher delivers through (issue #17).

A :class:`Channel` carries ``ResourceEvent`` values from the process that handled
a write to every subscriber. It is a ``DiscriminatedUnionMixin``, so a deployment
selects a concrete channel by ``kind`` with no code change.

Two implementations: :class:`InMemoryChannel` (the single-process default) and
``RedisChannel`` (clustered, over ``redis.asyncio`` pub/sub, imported lazily).

The publisher is ``RedisTrigger`` despite the name: the *trigger* seam fires the
publish after a successful write and forwards the built event to whichever
``Channel`` it was configured with; ``Channel`` itself knows nothing about
triggers. The **same instance** must be passed to the publishing trigger and to
``add_realtime``.

A channel is its own async context manager (entered through the manifest's
``managers`` slot), so a channel that owns a client ties that client's lifetime
to the app.

Delivery is **at-most-once**; the reconnect reconcile is a normal REST
``search``.

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

# The per-subscriber buffer bound for the in-memory channel.
DEFAULT_QUEUE_MAXSIZE = 100


class Channel(DiscriminatedUnionMixin, ABC):
    """The abstract publish / subscribe seam, discriminated by ``kind``."""

    async def __aenter__(self) -> Channel:
        """Open the channel's client / connection (override to do real work)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the channel's client / connection (override to do real work)."""
        return None

    @abstractmethod
    async def publish(self, event: ResourceEvent) -> None:
        """Deliver ``event`` to every subscriber (at-most-once, unordered).

        Called only after the write it reports on has committed.
        """

    @abstractmethod
    def subscribe(self) -> AsyncIterator[ResourceEvent]:
        """An async iterator of events published to this channel.

        Each subscriber gets its own iterator, so several WebSocket clients fan
        out independently.
        """


class InMemoryChannel(Channel):
    """The single-process default: an in-process fan-out of published events.

    Each subscriber gets a fresh **bounded** queue that drops its **oldest**
    event on overflow, so one slow subscriber cannot grow an unbounded buffer.
    """

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
