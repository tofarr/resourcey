"""``RedisChannel`` — the clustered pub/sub channel (issue #17).

Redis pub/sub bridges **processes**, not durability. Each app instance publishes
to a Redis channel and every instance re-delivers to *its* local subscribers.
Behind a load balancer a subscriber is usually on a different process from the
one that handled the write, so an in-process callback alone is not correct in the
deployment the framework assumes (multiple app instances, one database); Redis
is what makes the fan-out correct there.

Two consequences stated explicitly, because the transport cannot give more:

* **At-most-once, unordered, no replay.** Pub/sub drops an event for an instance
  that is down or briefly disconnected, and there is no offset to resume from. Do
  not promise ordering or delivery guarantees the transport cannot give.
* **Policy filtering is local.** A published event carries no principal and no
  filter; each instance re-applies *its own* subscribers' policies before send.
  A process never trusts another process's filtering — the policy is never
  serialized to Redis.

``redis`` is imported **lazily**, only when a real client is built (or an explicit
``client=`` is supplied), exactly as ``filestore`` imports ``boto3`` lazily, so
``realtime`` imports without the extra and a missing driver fails with an
actionable :class:`ImportError` naming ``resourcey[redis]``. The client is owned
by the channel's ``__aenter__`` / ``__aexit__`` (entered through the manifest's
``managers`` slot) when the channel built it; an injected ``client=`` is the
escape hatch and is not closed.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers (and ``redis`` lazily).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from resourcey.realtime.realtime_channel import Channel
from resourcey.realtime.realtime_event import ResourceEvent

# The Redis channel name events are published to. One name is enough: the event
# carries its resource, and a subscriber filters locally, so a per-resource
# channel would only add subscribe churn.
DEFAULT_REDIS_CHANNEL = "resourcey:events"


def _require_redis() -> Any:
    """Import ``redis.asyncio`` or fail with an actionable message naming the extra."""
    try:
        from redis import asyncio as redis_asyncio
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "RedisChannel requires the 'redis' extra. Install it with "
            "`pip install resourcey[redis]` or `uv sync --extra redis`."
        ) from exc
    return redis_asyncio


class RedisChannel(Channel):
    """A cross-process channel over ``redis.asyncio`` pub/sub.

    Attributes:
        url: The Redis connection URL (``redis://host:port/db``). Used only when
            no explicit ``client`` is supplied.
        channel_name: The Redis channel name events are published to.
        client: An optional pre-built async Redis client (the escape hatch). When
            supplied it wins over ``url`` and is **not** closed on exit — the
            caller owns it.
    """

    url: str = "redis://localhost:6379/0"
    channel_name: str = DEFAULT_REDIS_CHANNEL
    client: Any = None

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._owns_client = False

    async def __aenter__(self) -> RedisChannel:
        """Build the client (lazily importing ``redis``) unless one was injected."""
        if self.client is None:
            redis_asyncio = _require_redis()
            self.client = redis_asyncio.from_url(self.url, decode_responses=False)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close a client this channel built; leave an injected one alone."""
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
            self._owns_client = False

    async def publish(self, event: ResourceEvent) -> None:
        """Publish ``event`` (JSON) to the Redis channel.

        The event is serialized with its read-model projection; a subscriber on
        any instance decodes it and re-applies its own policies before delivering.
        """
        if self.client is None:
            raise RuntimeError("RedisChannel used before entering it (no client)")
        await self.client.publish(self.channel_name, event.model_dump_json())

    def subscribe(self) -> AsyncIterator[ResourceEvent]:
        """An iterator of events this instance's Redis subscription receives."""
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ResourceEvent]:
        if self.client is None:  # pragma: no cover - guarded by the caller entering first
            raise RuntimeError("RedisChannel used before entering it (no client)")
        pubsub = self.client.pubsub()
        await pubsub.subscribe(self.channel_name)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield ResourceEvent.model_validate_json(message["data"])
        finally:
            await pubsub.unsubscribe(self.channel_name)
            await pubsub.aclose()
