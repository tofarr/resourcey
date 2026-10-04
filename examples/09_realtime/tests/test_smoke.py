"""Smoke test for the realtime-example app's channel wiring.

Exercises the full stack — a REST write delivering a WebSocket event — against
an in-memory SQLite database and a real channel; no mocking of the realtime
mechanism itself. Storage is injected through the ``session_factory=`` escape
hatch (a shared in-memory engine), which keeps this suite fast and independent
of the committed migration — the migration itself is verified in
``test_e2e.py``.

``background=False`` is used throughout so a trigger's publish is guaranteed
to have already happened by the time the REST response returns, without
polling the event loop. ``fastapi.testclient.TestClient`` drives both the
REST calls and the WebSocket connection — its context manager runs the app's
lifespan (entering the manifest, including the shared channel), so delivery
is genuinely end to end: a real ASGI route dispatch, a real
``TriggeredService``, a real channel fan-out, a real WebSocket frame.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from realtime_example.app import build_app
from realtime_example.models import Base
from resourcey.realtime.realtime_channel import Channel, InMemoryChannel


def _client_for(channel: Channel) -> Iterator[TestClient]:
    """A ``TestClient`` over an app backed by a shared in-memory SQLite engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_tables())

    _manifest, app, _channel = build_app(session_factory=maker, channel=channel, background=False)
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose())


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The default-channel (``InMemoryChannel``) variant."""
    yield from _client_for(InMemoryChannel())


@pytest.fixture
def redis_client() -> Iterator[TestClient]:
    """The same app and tests, over a real ``RedisChannel`` (``fakeredis``).

    Proves the ``CHANNEL_CLASS`` swap documented in ``.env`` / the README
    integrates correctly with the app's trigger + WebSocket wiring — not a
    second, parallel implementation to maintain, the exact same
    ``build_app(channel=...)`` seam with a different channel instance.
    """
    fakeredis = pytest.importorskip("fakeredis")
    redis_client_ = fakeredis.aioredis.FakeRedis()
    from resourcey.realtime.realtime_redis_channel import RedisChannel

    yield from _client_for(RedisChannel(client=redis_client_))


class TestCreateUpdateDelete:
    def test_create_delivers_an_event_to_a_subscriber(self, client: TestClient) -> None:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "threads"})
            assert ws.receive_json() == {"type": "ack", "resource": "threads"}

            resp = client.post("/threads", json={"title": "Hello", "description": "world"})
            assert resp.status_code == 201
            thread = resp.json()

            event = ws.receive_json()
            assert event["type"] == "event"
            assert event["event"]["resource"] == "threads"
            assert event["event"]["kind"] == "created"
            assert event["event"]["id"] == thread["id"]
            assert event["event"]["item"]["title"] == "Hello"

    def test_update_and_delete_deliver_events_too(self, client: TestClient) -> None:
        thread = client.post("/threads", json={"title": "T"}).json()

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "threads"})
            ws.receive_json()  # ack

            resp = client.patch(f"/threads/{thread['id']}", json={"title": "Edited"})
            assert resp.status_code == 200
            event = ws.receive_json()
            assert event["event"]["kind"] == "updated"
            assert event["event"]["item"]["title"] == "Edited"

            resp = client.delete(f"/threads/{thread['id']}")
            assert resp.status_code == 204
            event = ws.receive_json()
            assert event["event"]["kind"] == "deleted"
            assert event["event"]["id"] == thread["id"]
            assert event["event"]["item"] is None

    def test_a_failed_write_delivers_nothing(self, client: TestClient) -> None:
        """A 404 update fires no trigger, so nothing is published either."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "threads"})
            ws.receive_json()  # ack

            resp = client.patch("/threads/9999", json={"title": "nope"})
            assert resp.status_code == 404

            # The next thing to arrive is a *real* event, not the 404 above --
            # proving the failed write really published nothing.
            ok = client.post("/threads", json={"title": "the real one"}).json()
            event = ws.receive_json()
            assert event["event"]["id"] == ok["id"]


class TestSubscriptionFiltering:
    def test_filter_scopes_delivery_to_matching_rows(self, client: TestClient) -> None:
        t1 = client.post("/threads", json={"title": "T1"}).json()
        t2 = client.post("/threads", json={"title": "T2"}).json()

        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "resource": "messages",
                    "filter": {"thread_id__eq": t1["id"]},
                }
            )
            ws.receive_json()  # ack

            client.post("/messages", json={"thread_id": t2["id"], "text": "not delivered"})
            client.post("/messages", json={"thread_id": t1["id"], "text": "delivered"})

            # Only the matching message ever reaches this connection -- the
            # hub filters before sending, so the t2 message produced no frame
            # at all (not merely one this assertion skips past).
            event = ws.receive_json()
            assert event["event"]["item"]["text"] == "delivered"

    def test_subscribing_to_one_resource_never_delivers_another(self, client: TestClient) -> None:
        thread = client.post("/threads", json={"title": "T"}).json()

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "resource": "messages"})
            ws.receive_json()  # ack

            # A threads write while only subscribed to messages...
            client.patch(f"/threads/{thread['id']}", json={"title": "Edited"})
            # ...then a genuine messages write, which must be the *first*
            # thing to arrive.
            client.post("/messages", json={"thread_id": thread["id"], "text": "hi"})
            event = ws.receive_json()
            assert event["event"]["resource"] == "messages"


class TestRedisChannelVariant:
    """The identical create/delivery flow, over a real ``RedisChannel``."""

    def test_create_delivers_an_event_over_redis(self, redis_client: TestClient) -> None:
        with redis_client.websocket_connect("/ws") as ws:
            # Let the hub's own channel.subscribe() (fakeredis's pubsub.subscribe())
            # finish registering before anything is published -- mirrors the
            # framework's own RedisChannel tests (see test_realtime.py).
            time.sleep(0.2)
            ws.send_json({"type": "subscribe", "resource": "threads"})
            ws.receive_json()  # ack

            resp = redis_client.post("/threads", json={"title": "Over Redis"})
            assert resp.status_code == 201

            event = ws.receive_json()
            assert event["event"]["kind"] == "created"
            assert event["event"]["item"]["title"] == "Over Redis"
