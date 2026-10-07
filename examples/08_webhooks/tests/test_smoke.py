"""Smoke test for the webhooks-example app's trigger wiring.

Exercises the full HTTP path (create / update / delete / batch-edit) against
an in-memory SQLite database via httpx's ASGI transport, asserting on the
*real* webhook receiver's log lines via ``caplog`` -- no mocking of the
trigger mechanism itself. Storage is injected through the ``session_factory=``
escape hatch (a shared in-memory engine), which keeps this suite fast and
independent of the committed migration -- the migration itself is verified in
``test_e2e.py``.

Delivery is genuinely over HTTP, not simulated: each ``WebhookTrigger`` is
bound (via :meth:`~resourcey.triggers.webhook_trigger.WebhookTrigger.bind_client`)
to an ``httpx.AsyncClient`` whose transport is the *same* app's ASGI callable,
so a trigger firing really does cross a (loopback, in-process) HTTP boundary
and land on ``webhook_receiver.py``'s real route handler -- there is no second
server only because ``ASGITransport`` makes one unnecessary for a test.

``background=False`` is used throughout so a trigger's log line is guaranteed
written before the response is asserted on, without polling the event loop.
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.triggered_dependency_builder import TriggeredDependencyBuilder
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.triggers.webhook_trigger import WebhookHeader, WebhookTrigger
from webhooks_example.message import MessageResource
from webhooks_example.models import Base, Message, Thread
from webhooks_example.webhook_receiver import register_webhook_routes

BASE_URL = "http://testserver"


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """An app over a shared in-memory SQLite database, both trigger rungs wired.

    Mirrors ``webhooks_example.app.build_app`` by hand (rather than importing
    it) so the test controls the triggers directly: ``threads`` wraps a plain
    ``SqlResource`` in ``TriggeredResource`` (the direct seam); ``messages``
    stays plain and gets its trigger from a ``TriggeredDependencyBuilder``
    built with an explicit ``resource_triggers=`` map (the config-driven seam,
    without needing ``APP_TRIGGERS_*`` env vars for this fast suite).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    audit_trigger = WebhookTrigger(
        url=f"{BASE_URL}/_webhooks/audit-log",
        headers=[WebhookHeader(name="X-Webhook-Secret", value=SecretStr("audit-secret"))],
    )
    slack_trigger = WebhookTrigger(url=f"{BASE_URL}/_webhooks/slack-notify")

    threads = TriggeredResource(
        SqlResource(Thread, session_factory=maker),
        on_edit=[audit_trigger],
        background=False,
    )
    messages = MessageResource(Message, session_factory=maker)
    builder = TriggeredDependencyBuilder(
        resource_triggers={"messages": [slack_trigger]},
        background=False,
    )

    manifest = Manifest(resources=[threads, messages], managers=[builder])
    app: FastAPI = create_app(manifest, dependency_builder=builder)
    register_webhook_routes(app)

    # Both triggers deliver back into this very app -- bind them to a client
    # whose transport *is* this app, after the app exists (see WebhookTrigger
    # .bind_client's docstring for why this is a two-step construction).
    transport = ASGITransport(app=app)
    webhook_client = AsyncClient(transport=transport, base_url=BASE_URL)
    audit_trigger.bind_client(webhook_client)
    slack_trigger.bind_client(webhook_client)

    # ASGITransport does not run the lifespan; enter the manifest manually so the
    # resources' runtime lifecycle is active for the requests below.
    await manifest.__aenter__()
    try:
        async with webhook_client, AsyncClient(transport=transport, base_url=BASE_URL) as c:
            yield c
    finally:
        await manifest.__aexit__(None, None, None)
        await engine.dispose()


_RECEIVER_LOGGER = "webhooks_example.webhook_receiver"


async def test_create_thread_fires_direct_trigger(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``threads``' directly-wired ``TriggeredResource`` really delivers over HTTP."""
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.post("/threads", json={"title": "Smoke test"})
    assert resp.status_code == 201
    assert "WEBHOOK RECEIVED -- subscriber='audit-log'" in caplog.text
    assert '"kind": "create"' in caplog.text
    assert '"title": "Smoke test"' in caplog.text
    # The configured X-Webhook-Secret header really arrived.
    assert "'x-webhook-secret': 'audit-secret'" in caplog.text


async def test_create_message_fires_config_driven_trigger(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``messages``' config-driven ``TriggeredDependencyBuilder`` really delivers over HTTP."""
    thread = (await client.post("/threads", json={"title": "T"})).json()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.post(
            "/messages", json={"thread_id": thread["id"], "text": "hello world"}
        )
    assert resp.status_code == 201
    assert "WEBHOOK RECEIVED -- subscriber='slack-notify'" in caplog.text
    assert '"text": "hello world"' in caplog.text


async def test_update_and_delete_fire_the_trigger(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    thread = (await client.post("/threads", json={"title": "T"})).json()
    message = (
        await client.post("/messages", json={"thread_id": thread["id"], "text": "old"})
    ).json()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.patch(f"/messages/{message['id']}", json={"text": "new"})
    assert resp.status_code == 200
    assert '"kind": "update"' in caplog.text
    assert '"text": "new"' in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.delete(f"/messages/{message['id']}")
    assert resp.status_code == 204
    assert '"kind": "delete"' in caplog.text
    assert f'"id": {message["id"]}' in caplog.text


async def test_batch_edit_fires_the_trigger_once(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A batch of three creates delivers **one** webhook request, not three."""
    thread = (await client.post("/threads", json={"title": "T"})).json()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.post(
            "/messages/batch-edit",
            json=[
                {"kind": "Create", "item": {"thread_id": thread["id"], "text": "first"}},
                {"kind": "Create", "item": {"thread_id": thread["id"], "text": "second"}},
                {"kind": "Create", "item": {"thread_id": thread["id"], "text": "third"}},
            ],
        )
    assert resp.status_code == 200
    webhook_records = [r for r in caplog.records if r.name == _RECEIVER_LOGGER]
    assert len(webhook_records) == 1
    assert "first" in webhook_records[0].message
    assert "second" in webhook_records[0].message
    assert "third" in webhook_records[0].message


async def test_a_missed_write_still_fires_the_trigger(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """An update of an absent id is a successful *no-write*, and the trigger still fires.

    Under the unified contract a miss is a ``200`` with a ``null`` body, not a
    ``404`` -- it is not an *error*, so the trigger fires exactly as for any
    other successful operation. ``WebhookTrigger`` reports the whole operation
    (``result: null`` marks the miss); contrast ``RedisTrigger``, which publishes
    nothing for a ``None`` update result because there is no row to report.
    """
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.patch("/messages/9999", json={"text": "nope"})
    assert resp.status_code == 200
    assert '"result": null' in caplog.text
    assert '"id": 9999' in caplog.text


async def test_reads_never_fire_the_trigger(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    thread = (await client.post("/threads", json={"title": "T"})).json()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_RECEIVER_LOGGER):
        resp = await client.get(f"/threads/{thread['id']}")
    assert resp.status_code == 200
    assert caplog.text == ""
