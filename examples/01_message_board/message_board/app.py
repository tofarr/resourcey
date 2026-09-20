"""Message-board example app entry point.

``message_board.app:create_app`` assembles a runnable FastAPI app via
:func:`resourcey.app.create_app`, using :class:`MessageBoardConfig` (SQLite by
default) and the two registered resources. Run with::

    resourcey run

or directly with uvicorn::

    uvicorn message_board.app:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from resourcey.app import create_app as _create_framework_app
from resourcey.config.config_runtime import set_config

# Importing the resources module registers Thread + Message before the app
# factory reads the resource set, so their tables are in metadata.
import message_board.resources  # noqa: F401
from message_board.config import MessageBoardConfig
from message_board.message import Message
from message_board.thread import Thread


def create_app() -> FastAPI:
    """Assemble the message-board FastAPI app (SQLite by default)."""
    set_config(MessageBoardConfig())
    return _create_framework_app(resources=[Thread, Message])
