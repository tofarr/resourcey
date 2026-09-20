"""Message-board example app entry point.

``message_board.app:create_app`` assembles a runnable FastAPI app via
:func:`resourcey.app.create_app`, using the stock :class:`FrameworkConfig`
(configured to SQLite via ``.env``) and the two registered resources. Run with::

    resourcey run

or directly with uvicorn::

    uvicorn message_board.app:create_app --factory --reload
"""

from __future__ import annotations

# Importing the resources module registers Thread + Message before the app
# factory reads the resource set, so their tables are in metadata.
import message_board.resources  # noqa: F401
from message_board.message import Message
from message_board.thread import Thread
from fastapi import FastAPI

from resourcey.app import create_app as _create_framework_app


def create_app() -> FastAPI:
    """Assemble the message-board FastAPI app (SQLite via .env)."""
    return _create_framework_app(resources=[Thread, Message])
