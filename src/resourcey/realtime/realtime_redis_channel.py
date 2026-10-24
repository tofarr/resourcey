"""``RedisChannel`` — the clustered pub/sub channel (issue #17).

Redis pub/sub bridges **processes**, not durability. Each app instance publishes
to a Redis channel and every instance re-delivers to *its* local subscribers.
Behind a load balancer a subscriber is usually on a different process from the one
that handled the write, so an in-process fan-out alone is not correct there.

Two consequences stated explicitly:

* **At-most-once, unordered, no replay.** Pub/sub drops an event for an instance
  that is down or briefly disconnected.
* **Policy filtering is local.** A published event carries no principal and no
  filter; each instance re-applies *its own* subscribers' policies before send.

``redis`` is imported **lazily**, so ``realtime`` imports without the extra and a
missing driver fails with an actionable ``ImportError`` naming
``resourcey[redis]``. A client the channel built is closed on exit; an injected
``client=`` is the escape hatch and is left alone.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers (and ``redis`` lazily).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import PrivateAttr

from resourcey.realtime.realtime_channel import Channel
from resourcey.realtime.realtime_event import ResourceEvent

# The Redis channel name events are published to. One name is enough: the
# event carries its resource, and a subscriber filters locally, so a
# per-resource channel would only add subscribe churn.
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

    ``url`` is used only when no explicit ``client`` is supplied. The injected
    client is a constructor-only keyword held in a ``PrivateAttr``, so it never
    reaches ``from_env``'s field walk.
    """

    url: str = "redis://localhost:6379/0"
    channel_name: str = DEFAULT_REDIS_CHANNEL

    _client: Any = PrivateAttr(default=None)
    _owns_client: bool = PrivateAttr(default=False)

    def __init__(self, *, client: Any = None, **data: Any) -> None:
        super().__init__(**data)
        self._client = client
        self._owns_client = False

    @property
    def client(self) -> Any:
        """The underlying ``redis.asyncio`` client, or ``None`` before entering."""
        return self._client

    async def __aenter__(self) -> RedisChannel:
        """Build the client (lazily importing ``redis``) unless one was injected."""
        if self._client is None:
            redis_asyncio = _require_redis()
            self._client = redis_asyncio.from_url(self.url, decode_responses=False)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close a client this channel built; leave an injected one alone."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    async def publish(self, event: ResourceEvent) -> None:
        """Publish ``event`` (JSON) to the Redis channel.

        A subscriber on any instance decodes it and re-applies its own policies.
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
