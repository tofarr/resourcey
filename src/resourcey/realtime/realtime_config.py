"""``RealtimeConfig`` — env-driven channel selection (issue #17).

``channel`` is a ``CHANNEL_CLASS``-selected ``LazyField`` (the same mechanism
``FileStoreConfig`` uses for its medium): unset selects ``InMemoryChannel`` with
no new dependency; set to a dotted path, the selected channel's own fields then
parse under the ``CHANNEL_`` prefix (e.g. ``CHANNEL_URL`` for a ``RedisChannel``).

``heartbeat_seconds`` is how often the subscriber socket sends a keepalive.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from typing import ClassVar

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.realtime.realtime_channel import Channel, InMemoryChannel

DEFAULT_HEARTBEAT_SECONDS = 30


class RealtimeConfig(BaseConfig):
    """The realtime config block: the selected channel plus socket tuning."""

    channel: ClassVar[Channel] = LazyField(default=InMemoryChannel)  # type: ignore[assignment]
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
