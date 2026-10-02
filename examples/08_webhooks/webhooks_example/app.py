"""Webhooks example app entry point (issue #18, built on ``resourcey.triggers``).

The same ``Thread`` / ``Message`` board as example 01, with a
:class:`~webhooks_example.triggers.LoggingWebhookTrigger` attached to each
resource — proving `#155 <https://github.com/tofarr/resourcey/issues/155>`__'s
edit-event trigger framework end to end, the way `#18
<https://github.com/tofarr/resourcey/issues/18>`__ originally asked for.
Because the trigger only **logs** what it would deliver (see
:mod:`webhooks_example.triggers`), this demo needs no second server, no
network access, and no mocking: watch the console while you make requests.

This app deliberately demonstrates **both** ways to attach a trigger, on two
different resources, so it doubles as the reference for choosing between them:

* ``threads`` — the **direct, no-config** seam. The resource declaration
  itself wraps the plain ``SqlResource`` in
  :class:`~resourcey.triggers.triggered_resource.TriggeredResource`::

      TriggeredResource(SqlResource(Thread, ...), on_edit=[LoggingWebhookTrigger(...)])

* ``messages`` — the **opt-in, env-driven** rung. The resource declaration in
  :mod:`webhooks_example.message` is untouched; the trigger is attached by
  :class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`,
  built from :class:`~resourcey.triggers.trigger_config.TriggerConfig` (parsed
  from ``APP_TRIGGERS_*`` — see ``.env``) and passed as ``create_app``'s
  ``dependency_builder=``. The *same* builder is handed every resource; for
  ``threads`` (already a ``TriggeredResource``) it passes the request straight
  through unchanged — see
  :meth:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder.get_service_dependency`.

The builder is also listed in the manifest's ``managers=`` (alongside the
session manager) so its per-resource ``TriggerRunner``s are drained on
shutdown, the same graceful-shutdown guarantee ``TriggeredResource`` gives
``threads`` for free via its own lifecycle.

Run with::

    uvicorn webhooks_example.app:app --env-file .env --port 8088

Note the ``--env-file``: the framework does no ``.env`` loading of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.trigger_config import TriggerConfig
from resourcey.triggers.triggered_dependency_builder import TriggeredDependencyBuilder
from resourcey.triggers.triggered_resource import TriggeredResource
from webhooks_example.message import MessageResource
from webhooks_example.models import Message, Thread
from webhooks_example.triggers import LoggingWebhookTrigger

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
    trigger_config: TriggerConfig | None = None,
    background: bool = True,
) -> tuple[Manifest, FastAPI, TriggeredDependencyBuilder]:
    """Build the manifest + FastAPI app, wiring both trigger rungs.

    Kept as a factory (rather than wiring only at import time) so the tests
    can inject an isolated ``session_factory`` / ``session_manager`` and a
    fresh ``trigger_config`` without touching the declarations.
    ``trigger_config`` defaults to :meth:`TriggerConfig.get_instance`, which
    reads the committed ``.env``'s ``APP_TRIGGERS_*`` entries. ``background``
    (default ``True``, matching the framework default) is exposed as a
    constructor knob purely so a test can force synchronous firing instead of
    polling the event loop for a background task to complete.
    """
    if session_factory is not None:
        session_kwargs: dict[str, Any] = {"session_factory": session_factory}
        managers: list[Any] = []
    else:
        manager = session_manager or default_session_manager
        session_kwargs = {"session_manager": manager}
        managers = [manager]

    # threads: the direct, no-config seam -- the trigger rides on the
    # resource declaration itself.
    threads = TriggeredResource(
        SqlResource(Thread, **session_kwargs),
        on_edit=[LoggingWebhookTrigger(name="audit-log")],
        background=background,
    )

    # messages: left as a plain resource. Its trigger (if any) comes from the
    # config-driven builder below -- no change to this declaration.
    messages = MessageResource(Message, **session_kwargs)

    config = trigger_config if trigger_config is not None else TriggerConfig.get_instance()
    builder = TriggeredDependencyBuilder.from_config(config, background=background)
    # Drained on manifest exit alongside the session manager -- see the
    # module docstring.
    managers.append(builder)

    manifest = Manifest(resources=[threads, messages], managers=managers)
    app = create_app(manifest, dependency_builder=builder)
    return manifest, app, builder


manifest, app, trigger_builder = build_app()
