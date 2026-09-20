"""Register the message-board resources with the framework.

Importing this module imports both resource classes so they are available to
the app factory. Unlike the SQL example, Mongo resources do not need eager
ORM model materialisation — there is no table metadata to register.
"""

from __future__ import annotations

from message_board.message import Message
from message_board.thread import Thread

__all__ = ["Message", "Thread"]
