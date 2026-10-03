"""Webhooks example app entry point (issue #18, built on ``resourcey.triggers``).

The same ``Thread`` / ``Message`` board as example 01, with a
:class:`~resourcey.triggers.webhook_trigger.WebhookTrigger` — the framework's
own, generic HTTP-delivering trigger — attached to each resource, proving
`#155 <https://github.com/tofarr/resourcey/issues/155>`__'s edit-event trigger
framework end to end, the way `#18
<https://github.com/tofarr/resourcey/issues/18>`__ originally asked for.

A real deployment's webhook would notify a separate, independently owned
service. To stay a **working**, runnable example with nothing external to
stand up, this app mounts its own receiving endpoint
(:mod:`webhooks_example.webhook_receiver`) on the *same* FastAPI app, and both
configured ``WebhookTrigger``\\ s point back at it over real HTTP (loopback
when run with ``uvicorn``, or the same ASGI app via ``httpx``'s in-process
transport in the test suite — see ``tests/``). This is a genuine delivery, not
a simulation: watch the console for the receiver's ``WEBHOOK RECEIVED`` log
lines while you make requests.

This app deliberately demonstrates **both** ways to attach a trigger, on two
different resources, so it doubles as the reference for choosing between them:

* ``threads`` — the **direct, no-config** seam. The resource declaration
  itself wraps the plain ``SqlResource`` in
  :class:`~resourcey.triggers.triggered_resource.TriggeredResource`::

      TriggeredResource(SqlResource(Thread, ...), on_edit=[WebhookTrigger(...)])

* ``messages`` — the **opt-in, env-driven** rung. The resource declaration in
  :mod:`webhooks_example.message` is untouched; the trigger is attached by
  :class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`,
  built from :class:`~resourcey.triggers.trigger_config.TriggerConfig` (parsed
  from ``APP_TRIGGERS_*`` — see ``.env``, which configures a
  ``WebhookTrigger`` by dotted path like any other trigger kind) and passed as
  ``create_app``'s ``dependency_builder=``. The *same* builder is handed every
  resource; for ``threads`` (already a ``TriggeredResource``) it passes the
  request straight through unchanged — see
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

import os
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.trigger_config import TriggerConfig
from resourcey.triggers.triggered_dependency_builder import TriggeredDependencyBuilder
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.triggers.webhook_trigger import (
    ExponentialBackoffRetry,
    WebhookHeader,
    WebhookTrigger,
)
from webhooks_example.message import MessageResource
from webhooks_example.models import Message, Thread
from webhooks_example.webhook_receiver import register_webhook_routes

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())

# The app's own externally-reachable base URL -- the directly-wired `threads`
# trigger below points back at it (the config-driven `messages` trigger reads
# its own URL from `.env`, which points at the same host/port). Matches the
# port `uvicorn ... --port 8088` is run on (see README.md /
# .vscode/launch.json); override for a different port or host.
DEFAULT_BASE_URL = os.environ.get("WEBHOOKS_EXAMPLE_BASE_URL", "http://127.0.0.1:8088")


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
    trigger_config: TriggerConfig | None = None,
    background: bool = True,
    base_url: str | None = None,
) -> tuple[Manifest, FastAPI, TriggeredDependencyBuilder, list[WebhookTrigger]]:
    """Build the manifest + FastAPI app, wiring both trigger rungs.

    Kept as a factory (rather than wiring only at import time) so the tests
    can inject an isolated ``session_factory`` / ``session_manager`` and a
    fresh ``trigger_config`` without touching the declarations.
    ``trigger_config`` defaults to :meth:`TriggerConfig.get_instance`, which
    reads the committed ``.env``'s ``APP_TRIGGERS_*`` entries. ``background``
    (default ``True``, matching the framework default) is exposed as a
    constructor knob purely so a test can force synchronous firing instead of
    polling the event loop for a background task to complete. ``base_url``
    overrides :data:`DEFAULT_BASE_URL` for the directly-wired ``threads``
    trigger -- the test suite points it at its own in-process ASGI transport
    instead of a real loopback address.

    Returns the directly-wired ``threads`` trigger alongside anything
    ``trigger_config`` resolved for ``messages``, in that order, so a caller
    that needs real network delivery to work against an app that does not yet
    exist (this one) can bind each a client *after* the app is built — see
    :meth:`~resourcey.triggers.webhook_trigger.WebhookTrigger.bind_client` and
    ``tests/test_smoke.py`` / ``tests/test_e2e.py``.
    """
    if session_factory is not None:
        session_kwargs: dict[str, Any] = {"session_factory": session_factory}
        managers: list[Any] = []
    else:
        manager = session_manager or default_session_manager
        session_kwargs = {"session_manager": manager}
        managers = [manager]

    resolved_base_url = base_url if base_url is not None else DEFAULT_BASE_URL

    # threads: the direct, no-config seam -- the trigger rides on the
    # resource declaration itself. The shared-secret header and the retry
    # strategy are both exercised here purely to demonstrate the fields;
    # the receiver (webhook_receiver.py) only logs what arrived.
    threads_trigger = WebhookTrigger(
        url=f"{resolved_base_url}/_webhooks/audit-log",
        headers=[WebhookHeader(name="X-Webhook-Secret", value=SecretStr("audit-log-dev-secret"))],
        retry=ExponentialBackoffRetry(max_retries=3, initial_delay_seconds=0.5),
    )
    threads = TriggeredResource(
        SqlResource(Thread, **session_kwargs),
        on_edit=[threads_trigger],
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
    register_webhook_routes(app)

    webhook_triggers: list[WebhookTrigger] = [threads_trigger]
    for triggers in config.resource_triggers().values():
        webhook_triggers.extend(t for t in triggers if isinstance(t, WebhookTrigger))
    return manifest, app, builder, webhook_triggers


manifest, app, trigger_builder, webhook_triggers = build_app()
