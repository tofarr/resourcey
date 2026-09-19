"""Register the message-board resources with the framework.

Importing this module imports both resource classes and registers them via
``register_resource``, which eagerly builds their SQLAlchemy models so the
derived tables land in ``ResourceyBase.metadata`` before migrations or table
creation run. The app, the migrations CLI, and any escape-hatch engine read
the registry (or ``RESOURCEY_RESOURCES``) from one source of truth.
"""

from __future__ import annotations

from examples.message_board.message import Message
from examples.message_board.thread import Thread

from resourcey.resource.registry import register_resource

register_resource(Thread)
register_resource(Message)
