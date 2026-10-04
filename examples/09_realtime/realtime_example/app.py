"""Realtime example app entry point (issue #17, built on ``resourcey.realtime``
and ``resourcey.triggers.redis_trigger``).

The same ``Thread`` / ``Message`` board as examples 01 and 08, with a
:class:`~resourcey.triggers.redis_trigger.RedisTrigger` attached to each
resource and a WebSocket subscription socket
(:func:`~resourcey.realtime.realtime_routes.add_realtime`) mounted on the
same app, proving `#17 <https://github.com/tofarr/resourcey/issues/17>`__'s
realtime channel end to end: a REST write publishes a typed event, and every
subscribed WebSocket client with permission to read the affected row receives
it, live.

Unlike `08_webhooks`, both resources are wired the **same** way — directly,
via ``TriggeredResource(..., on_edit=[RedisTrigger(channel=channel)])`` —
rather than splitting between the direct and config-driven rungs. That split
is already 08's job; here it would only obscure a real constraint this
feature has: the **same ``Channel`` instance** must reach every publishing
trigger and :func:`add_realtime` itself, because for the default
``InMemoryChannel`` that instance **is** the single process's fan-out (a
second, independently-constructed ``InMemoryChannel`` would publish into a
fan-out nothing subscribes to). So this app builds exactly **one** channel —
``RealtimeConfig.get_instance().channel``, the same ``CHANNEL_CLASS``-selected
LazyField ``FileStoreConfig`` uses for its medium (default
:class:`~resourcey.realtime.realtime_channel.InMemoryChannel`, no new
dependency; set ``CHANNEL_CLASS=resourcey.realtime.realtime_redis_channel.RedisChannel``
in ``.env`` to bridge real processes over Redis instead) — and threads that
one object everywhere: into each resource's ``RedisTrigger``, into
``add_realtime``, and into ``Manifest(managers=[...])`` so its lifecycle (a
``RedisChannel`` owns a real client) is tied to the app.

This app runs with the framework's default, no-authentication
``OpenDependencyBuilder`` (like example 01) so the demo has zero setup: any
WebSocket client can subscribe to anything any REST caller could read. The
realtime socket's authorization is not a separate mechanism bolted on top —
it is the **same** ``dependency_builder=`` seam REST already uses (see
:func:`~resourcey.realtime.realtime_routes.add_realtime`'s
``dependency_builder=``), so composing this example with ``03_api_key_auth``'s
``AuthorizedDependencyBuilder`` (or ``04_simple_roles`` / ``05_full_rbac``'s
row-scoped policies) is a one-line change — pass the very same builder to
both ``create_app`` and ``add_realtime`` — and get a socket that authenticates
the handshake and re-filters every event per subscriber's resolved policy,
with no realtime-specific authorization code to write. See the README's
"Composing with auth" section.

Run with::

    uvicorn realtime_example.app:app --env-file .env --port 8089

Note the ``--env-file``: the framework does no ``.env`` loading of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from realtime_example.models import Message, Thread
from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.realtime.realtime_asyncapi import add_asyncapi
from resourcey.realtime.realtime_channel import Channel
from resourcey.realtime.realtime_config import RealtimeConfig
from resourcey.realtime.realtime_routes import add_realtime
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.redis_trigger import RedisTrigger
from resourcey.triggers.triggered_resource import TriggeredResource

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
    channel: Channel | None = None,
    background: bool = True,
) -> tuple[Manifest, FastAPI, Channel]:
    """Build the manifest + FastAPI app, wiring the realtime channel.

    Kept as a factory (rather than wiring only at import time) so the tests
    can inject an isolated ``session_factory=`` / ``session_manager=`` and a
    fresh ``channel=`` (e.g. a ``RedisChannel`` over ``fakeredis``) without
    touching the declarations. ``channel`` defaults to
    ``RealtimeConfig.get_instance().channel`` (``CHANNEL_CLASS``-selected, see
    the module docstring). ``background`` (default ``True``, matching the
    framework default) is exposed as a constructor knob purely so a test can
    force synchronous publishing instead of polling the event loop for a
    background task to complete.

    Returns the manifest, the app, and the resolved channel — a caller that
    wants to open a second WebSocket connection directly against the channel
    (bypassing HTTP) can reuse the same instance.
    """
    if session_factory is not None:
        session_kwargs: dict[str, Any] = {"session_factory": session_factory}
        managers: list[Any] = []
    else:
        manager = session_manager or default_session_manager
        session_kwargs = {"session_manager": manager}
        managers = [manager]

    resolved_channel = channel if channel is not None else RealtimeConfig.get_instance().channel
    # Entered/exited with the manifest, exactly like the session manager above --
    # a RedisChannel owns a real client whose lifecycle must match the app's.
    managers.append(resolved_channel)

    threads = TriggeredResource(
        SqlResource(Thread, **session_kwargs),
        on_edit=[RedisTrigger(channel=resolved_channel)],
        background=background,
    )
    messages = TriggeredResource(
        SqlResource(Message, **session_kwargs),
        on_edit=[RedisTrigger(channel=resolved_channel)],
        background=background,
    )

    manifest = Manifest(resources=[threads, messages], managers=managers)
    app = create_app(manifest)
    add_realtime(app, manifest, channel=resolved_channel)
    add_asyncapi(app, manifest)
    return manifest, app, resolved_channel


manifest, app, channel = build_app()
