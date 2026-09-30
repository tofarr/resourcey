"""``RealtimeConfig`` — env-driven channel selection (issue #17).

The channel is selected with no code change through a
:class:`~resourcey.config.lazy_field.LazyField` (the exact mechanism
``FileStoreConfig`` uses for its medium): ``CHANNEL_CLASS`` names a
:class:`~resourcey.realtime.realtime_channel.Channel` subclass by dotted path,
and the selected channel is a Pydantic model so its own fields parse from the
environment under the ``CHANNEL_`` prefix (e.g. ``CHANNEL_URL`` for a
:class:`~resourcey.realtime.realtime_redis_channel.RedisChannel`). With
``CHANNEL_CLASS`` unset the config falls back to
:class:`~resourcey.realtime.realtime_channel.InMemoryChannel`, so an app that
adds nothing gets a working single-process channel and no new dependency.

The one extra field, ``heartbeat_seconds``, is how often the subscriber socket
sends a keepalive so an idle connection is not reaped by an intermediary.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from typing import ClassVar

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.realtime.realtime_channel import Channel, InMemoryChannel

# How often the subscriber socket emits a keepalive.
DEFAULT_HEARTBEAT_SECONDS = 30


class RealtimeConfig(BaseConfig):
    """The realtime config block: the selected channel plus socket tuning.

    Attributes:
        channel: The selected channel instance, resolved lazily from
            ``CHANNEL_CLASS`` (default :class:`InMemoryChannel`).
        heartbeat_seconds: How often the subscriber socket sends a keepalive.
    """

    channel: ClassVar[Channel] = LazyField(default=InMemoryChannel)  # type: ignore[assignment]
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
