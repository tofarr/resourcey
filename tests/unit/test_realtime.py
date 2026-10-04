"""Tests for ``resourcey.realtime`` (issue #17).

Exercised against real code paths throughout: a real ``InMemoryChannel`` fan-out,
a real ``RedisChannel`` over ``fakeredis`` (an in-process Redis-protocol
double, not a mock of the channel itself), a real ``SqlResource`` /
``TriggeredResource`` / ``RedisTrigger`` write path, a real
``AuthorizedDependencyBuilder`` / ``RolePolicyResolver`` for the socket's
authorization tests, and a minimal duck-typed ``_FakeWebSocket`` standing in
for a live ASGI connection (the same "fake the one untestable transport
boundary" pattern ``test_webhook_trigger.py`` uses for ``httpx.AsyncClient``).
"""

from __future__ import annotations

import asyncio
import enum
import json
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, WebSocketDisconnect
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_policy import Owner, ReadOnly
from resourcey.auth.auth_principal import Principal, PrincipalKind
from resourcey.auth.auth_role import RolePolicyResolver
from resourcey.core.errors import InvalidInputError
from resourcey.core.manifest import Manifest
from resourcey.http.dependency_builder import OpenDependencyBuilder
from resourcey.realtime.realtime_asyncapi import (
    add_asyncapi,
    generate_asyncapi_document,
    get_asyncapi_html,
)
from resourcey.realtime.realtime_channel import InMemoryChannel
from resourcey.realtime.realtime_config import RealtimeConfig
from resourcey.realtime.realtime_event import EventKind, ResourceEvent
from resourcey.realtime.realtime_redis_channel import RedisChannel
from resourcey.realtime.realtime_routes import (
    DEFAULT_REALTIME_PATH,
    RealtimeHub,
    RealtimeRejectionError,
    _build_subscription_filter,
    _Connection,
    _deliverable,
    _is_unscoped,
    _read_filter,
    _serve_connection,
    add_realtime,
)
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.redis_trigger import RedisTrigger
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.util.search_filter import AllFilter, NoMatchFilter

# ---------------------------------------------------------------------------
# Fixtures: SQLite-backed resources shared by several test classes
# ---------------------------------------------------------------------------


class RealtimeBase(DeclarativeBase):
    pass


class Thread(RealtimeBase):
    __tablename__ = "realtime_threads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(100))
    # UUID (matching Principal.id's type) so an Owner policy's EqFilter(user_id)
    # compares like-for-like -- see test_row_scoped_policy_hides_other_principals_rows.
    author_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)


class Status(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class StatusBase(DeclarativeBase):
    pass


class Ticket(StatusBase):
    """A model with a nested (``$defs``) field, for the asyncapi rename test."""

    __tablename__ = "realtime_tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[Status] = mapped_column(default=Status.OPEN)


def _maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_async_engine("sqlite+aiosqlite:///:memory:"), expire_on_commit=False
    )


@pytest_asyncio.fixture
async def threads() -> AsyncIterator[SqlResource[Any, Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource: SqlResource[Any, Any] = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(RealtimeBase.metadata.create_all)
    yield resource
    await engine.dispose()


async def _next(iterator: AsyncIterator[ResourceEvent]) -> ResourceEvent:
    """``anext(iterator)`` as a genuine coroutine, so ``asyncio.create_task`` accepts it."""
    return await iterator.__anext__()


async def _until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true, failing the test if ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# ResourceEvent / EventKind
# ---------------------------------------------------------------------------


class TestResourceEvent:
    def test_round_trips_through_json(self) -> None:
        event = ResourceEvent(
            resource="threads", kind=EventKind.CREATED, id="1", item={"id": "1", "title": "hi"}
        )
        again = ResourceEvent.model_validate_json(event.model_dump_json())
        assert again == event

    def test_is_frozen(self) -> None:
        event = ResourceEvent(resource="threads", kind=EventKind.DELETED, id="1")
        with pytest.raises(ValidationError):
            event.resource = "other"


# ---------------------------------------------------------------------------
# InMemoryChannel
# ---------------------------------------------------------------------------


class TestInMemoryChannel:
    async def test_publish_fans_out_to_every_subscriber(self) -> None:
        channel = InMemoryChannel()
        async with channel:
            sub_a = channel.subscribe()
            sub_b = channel.subscribe()
            event = ResourceEvent(resource="threads", kind=EventKind.CREATED, id="1")
            task_a: asyncio.Task[ResourceEvent] = asyncio.create_task(_next(sub_a))
            task_b: asyncio.Task[ResourceEvent] = asyncio.create_task(_next(sub_b))
            await asyncio.sleep(0.01)
            await channel.publish(event)
            got_a = await asyncio.wait_for(task_a, timeout=2)
            got_b = await asyncio.wait_for(task_b, timeout=2)
            assert got_a == event
            assert got_b == event

    async def test_full_queue_drops_the_oldest_event(self) -> None:
        channel = InMemoryChannel(queue_maxsize=2)
        sub = channel.subscribe()
        for i in range(5):
            await channel.publish(ResourceEvent(resource="t", kind=EventKind.CREATED, id=str(i)))
        first = await sub.__anext__()
        second = await sub.__anext__()
        # Only the two most recent survive the bound (drop-oldest).
        assert {first.id, second.id} == {"3", "4"}

    async def test_aexit_ends_every_subscriber_iterator(self) -> None:
        channel = InMemoryChannel()
        sub = channel.subscribe()
        task: asyncio.Task[ResourceEvent] = asyncio.create_task(_next(sub))
        await asyncio.sleep(0.01)
        await channel.__aexit__(None, None, None)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(task, timeout=2)


# ---------------------------------------------------------------------------
# RedisChannel (over fakeredis -- a real Redis-protocol implementation)
# ---------------------------------------------------------------------------


class TestRedisChannel:
    async def test_publish_and_subscribe_round_trip(self) -> None:
        fakeredis = pytest.importorskip("fakeredis")
        server = fakeredis.FakeServer()
        client = fakeredis.aioredis.FakeRedis(server=server)
        channel = RedisChannel(client=client)
        async with channel:
            sub = channel.subscribe()
            recv: asyncio.Task[ResourceEvent] = asyncio.create_task(_next(sub))
            await asyncio.sleep(0.1)  # let pubsub.subscribe() register
            event = ResourceEvent(resource="threads", kind=EventKind.CREATED, id="1", item={"a": 1})
            await channel.publish(event)
            got = await asyncio.wait_for(recv, timeout=3)
            assert got == event

    async def test_injected_client_is_not_closed_on_exit(self) -> None:
        fakeredis = pytest.importorskip("fakeredis")
        client = fakeredis.aioredis.FakeRedis()
        channel = RedisChannel(client=client)
        async with channel:
            pass
        assert channel.client is client  # not torn down -- caller owns it

    async def test_builds_and_closes_its_own_client_when_none_is_injected(self) -> None:
        pytest.importorskip("redis")
        channel = RedisChannel(url="redis://localhost:63799/0")  # an unused local port
        async with channel:
            assert channel.client is not None
        assert channel.client is None  # a self-built client is closed on exit

    async def test_publish_before_entering_raises(self) -> None:
        channel = RedisChannel()
        with pytest.raises(RuntimeError, match="before entering"):
            await channel.publish(ResourceEvent(resource="t", kind=EventKind.CREATED, id="1"))

    async def test_missing_redis_dependency_names_the_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "redis", None)
        monkeypatch.setitem(sys.modules, "redis.asyncio", None)
        channel = RedisChannel()
        with pytest.raises(ImportError, match=r"resourcey\[redis\]"):
            await channel.__aenter__()


# ---------------------------------------------------------------------------
# RealtimeConfig
# ---------------------------------------------------------------------------


class TestRealtimeConfig:
    def test_defaults_to_an_in_memory_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # LazyField reads straight from the environment on first access --
        # unprefixed, unlike an ordinary BaseConfig field (see
        # test_heartbeat_seconds_default_and_override).
        monkeypatch.delenv("CHANNEL_CLASS", raising=False)
        config = RealtimeConfig()
        assert isinstance(config.channel, InMemoryChannel)

    def test_class_env_var_selects_a_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "CHANNEL_CLASS", "resourcey.realtime.realtime_redis_channel.RedisChannel"
        )
        monkeypatch.setenv("CHANNEL_URL", "redis://example:6380/2")
        config = RealtimeConfig()
        assert isinstance(config.channel, RedisChannel)
        assert config.channel.url == "redis://example:6380/2"

    def test_heartbeat_seconds_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An ordinary field parses only through get_instance() (env_parser,
        # under the process-wide prefix), unlike channel's LazyField above.
        monkeypatch.delenv("APP_HEARTBEAT_SECONDS", raising=False)
        RealtimeConfig.clear_instance_cache()
        assert RealtimeConfig.get_instance().heartbeat_seconds == 30
        monkeypatch.setenv("APP_HEARTBEAT_SECONDS", "5")
        RealtimeConfig.clear_instance_cache()
        assert RealtimeConfig.get_instance().heartbeat_seconds == 5
        RealtimeConfig.clear_instance_cache()


# ---------------------------------------------------------------------------
# Realtime routes -- a minimal duck-typed WebSocket double
# ---------------------------------------------------------------------------


class _Disconnect:
    pass


_DISCONNECT = _Disconnect()


class _FakeWebSocket:
    """A minimal duck-typed stand-in for ``fastapi.WebSocket``.

    Mirrors only what ``realtime_routes.py`` actually calls: ``headers`` /
    ``cookies`` (read by an ``Authenticator``), ``accept`` / ``close``, and
    ``receive_json`` / ``send_json``. Inbound messages are pushed onto an
    ``asyncio.Queue`` so a test can interleave pushing a message, letting the
    server react, and asserting -- the same realistic timing a live socket
    has, without a real ASGI server.
    """

    def __init__(self, *, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = headers or {}
        self.cookies: dict[str, str] = {}
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed_code: int | None = None
        self._inbound: asyncio.Queue[Any] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def receive_json(self) -> Any:
        message = await self._inbound.get()
        if message is _DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        return message

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def push(self, message: Any) -> None:
        self._inbound.put_nowait(message)

    def disconnect(self) -> None:
        self._inbound.put_nowait(_DISCONNECT)


def _events(ws: _FakeWebSocket) -> list[dict[str, Any]]:
    return [m for m in ws.sent if m.get("type") == "event"]


def _acks(ws: _FakeWebSocket) -> list[dict[str, Any]]:
    return [m for m in ws.sent if m.get("type") == "ack"]


def _errors(ws: _FakeWebSocket) -> list[dict[str, Any]]:
    return [m for m in ws.sent if m.get("type") == "error"]


class TestRealtimeHub:
    def _manifest(self, resource: SqlResource[Any, Any]) -> Manifest:
        return Manifest(resources=[resource])

    async def test_authenticate_with_the_open_builder_is_anonymous(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        principal = await hub.authenticate(_FakeWebSocket())
        assert principal is None

    async def test_authenticate_rejects_an_invalid_credential(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(), posture=Posture.OPTIONAL
        )
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=builder,
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket(headers={"X-API-Key": "nope"})
        with pytest.raises(RealtimeRejectionError, match="Invalid credential"):
            await hub.authenticate(ws)

    async def test_authenticate_requires_a_credential_under_required_posture(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        builder = AuthorizedDependencyBuilder(authenticator=ApiKeyAuthenticator())
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=builder,
            heartbeat_seconds=0,
        )
        with pytest.raises(RealtimeRejectionError, match="required"):
            await hub.authenticate(_FakeWebSocket())

    async def test_authenticate_with_no_credential_under_optional_posture_is_anonymous(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(), posture=Posture.OPTIONAL
        )
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=builder,
            heartbeat_seconds=0,
        )
        assert await hub.authenticate(_FakeWebSocket()) is None

    async def test_authenticate_resolves_a_valid_credentials_principal(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        from pydantic import SecretStr

        from resourcey.auth.auth_api_key_resource import config_api_key_resource
        from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig

        cfg = ApiKeysConfig(
            api_keys=[ApiKeyConfig(id="k1", name="one", key=SecretStr("secret-one"))]
        )
        key_resource = config_api_key_resource(cfg)
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(key_resource=key_resource)
        )
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=builder,
            heartbeat_seconds=0,
        )
        principal = await hub.authenticate(_FakeWebSocket(headers={"X-API-Key": "secret-one"}))
        assert principal is not None

    async def test_subscribe_rejects_an_invalid_filter(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        with pytest.raises(RealtimeRejectionError, match="Unknown filter"):
            await hub.subscribe(None, "threads", {"nope__eq": "x"})

    async def test_subscribe_rejects_an_unknown_resource(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        with pytest.raises(RealtimeRejectionError, match="Unknown resource"):
            await hub.subscribe(None, "nope", None)

    async def test_subscribe_resolves_policies_and_filter(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            self._manifest(threads),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        subscription = await hub.subscribe(None, "threads", {"title__eq": "hi"})
        assert subscription.resource == "threads"
        assert isinstance(subscription.policy_filter, AllFilter)
        assert subscription.sub_filter is not None


class TestBuildSubscriptionFilter:
    def test_none_and_empty_are_no_filter(self, threads: SqlResource[Any, Any]) -> None:
        assert _build_subscription_filter(threads, None) is None
        assert _build_subscription_filter(threads, {}) is None

    def test_unknown_field_is_rejected(self, threads: SqlResource[Any, Any]) -> None:
        with pytest.raises(InvalidInputError, match="Unknown filter"):
            _build_subscription_filter(threads, {"nope__eq": "x"})

    def test_non_object_is_rejected(self, threads: SqlResource[Any, Any]) -> None:
        with pytest.raises(InvalidInputError):
            _build_subscription_filter(threads, "not-a-dict")

    def test_uuid_eq_value_is_coerced(self, threads: SqlResource[Any, Any]) -> None:
        target = uuid.uuid4()
        search_filter = _build_subscription_filter(threads, {"id__eq": str(target)})
        assert search_filter is not None

    def test_in_accepts_a_json_array(self, threads: SqlResource[Any, Any]) -> None:
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        search_filter = _build_subscription_filter(threads, {"id__in": ids})
        assert search_filter is not None

    def test_invalid_value_is_rejected(self, threads: SqlResource[Any, Any]) -> None:
        with pytest.raises(InvalidInputError, match="Invalid value"):
            _build_subscription_filter(threads, {"id__eq": "not-a-uuid"})


class TestDeliverable:
    async def test_created_event_matches_sub_filter(self, threads: SqlResource[Any, Any]) -> None:
        subscription = await RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        ).subscribe(None, "threads", {"title__eq": "hi"})
        row_a, row_b = str(uuid.uuid4()), str(uuid.uuid4())
        author = str(uuid.uuid4())
        matching = ResourceEvent(
            resource="threads",
            kind=EventKind.CREATED,
            id=row_a,
            item={"id": row_a, "title": "hi", "author_id": author},
        )
        other = ResourceEvent(
            resource="threads",
            kind=EventKind.CREATED,
            id=row_b,
            item={"id": row_b, "title": "bye", "author_id": author},
        )
        assert _deliverable([subscription], matching) is True
        assert _deliverable([subscription], other) is False

    async def test_deleted_event_requires_unscoped_filters(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        unscoped = await hub.subscribe(None, "threads", None)
        scoped = await hub.subscribe(None, "threads", {"title__eq": "hi"})
        deleted = ResourceEvent(resource="threads", kind=EventKind.DELETED, id="1")
        assert _deliverable([unscoped], deleted) is True
        assert _deliverable([scoped], deleted) is False

    def test_is_unscoped(self) -> None:
        assert _is_unscoped(AllFilter()) is True
        assert _is_unscoped(NoMatchFilter()) is False

    async def test_event_for_a_different_resource_is_skipped(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        subscription = await RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        ).subscribe(None, "threads", None)
        other_resource_event = ResourceEvent(
            resource="items", kind=EventKind.CREATED, id="1", item={"id": "1"}
        )
        assert _deliverable([subscription], other_resource_event) is False

    async def test_an_item_that_fails_to_validate_is_skipped(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        subscription = await RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        ).subscribe(None, "threads", None)
        malformed = ResourceEvent(
            resource="threads", kind=EventKind.CREATED, id="1", item={"not": "a valid thread"}
        )
        assert _deliverable([subscription], malformed) is False


class TestConnectionSend:
    async def test_send_returns_false_when_the_socket_is_gone(self) -> None:
        class _GoneWebSocket:
            async def send_json(self, payload: dict[str, Any]) -> None:
                raise WebSocketDisconnect(code=1001)

        connection = _Connection(websocket=_GoneWebSocket())  # type: ignore[arg-type]
        assert await connection.send({"type": "ping"}) is False

    async def test_send_returns_true_on_success(self) -> None:
        ws = _FakeWebSocket()
        connection = _Connection(websocket=ws)  # type: ignore[arg-type]
        assert await connection.send({"type": "ping"}) is True
        assert ws.sent == [{"type": "ping"}]


class TestReadFilter:
    async def test_no_policies_is_fail_closed(self) -> None:
        search_filter = await _read_filter([], None)
        assert isinstance(search_filter, NoMatchFilter)

    async def test_read_only_policy_admits_everything(self) -> None:
        search_filter = await _read_filter([ReadOnly()], None)
        assert isinstance(search_filter, AllFilter)


# ---------------------------------------------------------------------------
# Full message dispatch / connection serving, over the fake WebSocket
# ---------------------------------------------------------------------------


class TestServeConnection:
    async def test_ping_gets_a_pong(self, threads: SqlResource[Any, Any]) -> None:
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push({"type": "ping"})
        await _until(lambda: any(m.get("type") == "pong" for m in ws.sent))
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_malformed_message_is_an_error(self, threads: SqlResource[Any, Any]) -> None:
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push(["not", "a", "dict"])
        await _until(lambda: len(_errors(ws)) == 1)
        assert "Malformed" in _errors(ws)[0]["message"]
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_subscribe_to_a_non_readable_resource_is_an_error(self) -> None:
        from resourcey.core.service import Action
        from resourcey.view.resource_view import ResourceView

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner: SqlResource[Any, Any] = SqlResource(Thread, session_factory=maker)
        write_only = ResourceView(resource=inner, exposed_actions=frozenset({Action.CREATE}))
        hub = RealtimeHub(
            Manifest(resources=[write_only]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        with pytest.raises(RealtimeRejectionError, match="not subscribable"):
            await hub.subscribe(None, "threads", None)

    async def test_unknown_message_type_is_an_error(self, threads: SqlResource[Any, Any]) -> None:
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push({"type": "bogus"})
        await _until(lambda: len(_errors(ws)) == 1)
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_subscribe_ack_then_unsubscribe_stops_delivery(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        channel = InMemoryChannel()
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            channel,
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push({"type": "subscribe", "resource": "threads"})
        await _until(lambda: len(_acks(ws)) == 1)

        row_1, row_2 = str(uuid.uuid4()), str(uuid.uuid4())
        author = str(uuid.uuid4())
        await channel.publish(
            ResourceEvent(
                resource="threads",
                kind=EventKind.CREATED,
                id=row_1,
                item={"id": row_1, "title": "t1", "author_id": author},
            )
        )
        await _until(lambda: len(_events(ws)) == 1)

        ws.push({"type": "unsubscribe", "resource": "threads"})
        await _until(lambda: len(_acks(ws)) == 2)

        await channel.publish(
            ResourceEvent(
                resource="threads",
                kind=EventKind.CREATED,
                id=row_2,
                item={"id": row_2, "title": "t2", "author_id": author},
            )
        )
        await asyncio.sleep(0.2)
        assert len(_events(ws)) == 1  # the second publish was not delivered

        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_subscribe_to_unknown_resource_sends_an_error(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            InMemoryChannel(),
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push({"type": "subscribe", "resource": "nope"})
        await _until(lambda: len(_errors(ws)) == 1)
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_end_to_end_write_through_redis_trigger_reaches_the_socket(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        """A real write through ``RedisTrigger`` -> ``InMemoryChannel`` -> the socket."""
        channel = InMemoryChannel()
        wrapped = TriggeredResource(threads, on_edit=[RedisTrigger(channel=channel)])
        hub = RealtimeHub(
            Manifest(resources=[wrapped]),
            channel,
            dependency_builder=OpenDependencyBuilder(),
            heartbeat_seconds=0,
        )
        ws = _FakeWebSocket()
        task = asyncio.create_task(_serve_connection(ws, hub, None))
        ws.push({"type": "subscribe", "resource": "threads"})
        await _until(lambda: len(_acks(ws)) == 1)

        dto_type = wrapped.get_dto_type()
        async with wrapped:
            service = await wrapped.get_service()
            async with service:
                created = await service.create(
                    dto_type.model_validate({"title": "hello", "author_id": str(uuid.uuid4())})
                )

        await _until(lambda: len(_events(ws)) == 1)
        event = _events(ws)[0]["event"]
        assert event["kind"] == "created"
        assert event["item"]["title"] == "hello"
        assert event["id"] == str(created.id)

        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)

    async def test_row_scoped_policy_hides_other_principals_rows(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        """An ``author``-scoped subscriber only receives their own rows.

        Mirrors the roles skill's "read all of X, own rows of Y" rule: the
        ``author`` role is scoped to ``Owner(owner_field="author_id")`` on
        ``threads`` only -- a real ``RolePolicyResolver``, not a double.
        """
        owner_id = uuid.uuid4()
        other_id = uuid.uuid4()
        resolver = RolePolicyResolver(
            resource_role_policies={"threads": {"author": [Owner(owner_field="author_id")]}},
        )
        channel = InMemoryChannel()
        builder = AuthorizedDependencyBuilder(policy_resolver=resolver)
        hub = RealtimeHub(
            Manifest(resources=[threads]),
            channel,
            dependency_builder=builder,
            heartbeat_seconds=0,
        )
        principal = Principal(id=owner_id, kind=PrincipalKind.USER, roles=frozenset({"author"}))
        subscription = await hub.subscribe(principal, "threads", None)

        row_1, row_2 = str(uuid.uuid4()), str(uuid.uuid4())
        own_event = ResourceEvent(
            resource="threads",
            kind=EventKind.CREATED,
            id=row_1,
            item={"id": row_1, "title": "mine", "author_id": str(owner_id)},
        )
        other_event = ResourceEvent(
            resource="threads",
            kind=EventKind.CREATED,
            id=row_2,
            item={"id": row_2, "title": "theirs", "author_id": str(other_id)},
        )
        assert _deliverable([subscription], own_event) is True
        assert _deliverable([subscription], other_event) is False


# ---------------------------------------------------------------------------
# add_realtime -- route registration
# ---------------------------------------------------------------------------


class TestAddRealtime:
    def test_mounts_a_websocket_route_at_the_default_path(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        router = add_realtime(app, Manifest(resources=[threads]), channel=InMemoryChannel())
        assert router.routes[0].path == DEFAULT_REALTIME_PATH
        assert router.routes[0].name == "realtime"

    def test_mounts_at_a_custom_path(self, threads: SqlResource[Any, Any]) -> None:
        app = FastAPI()
        router = add_realtime(
            app, Manifest(resources=[threads]), channel=InMemoryChannel(), path="/subscriptions"
        )
        assert router.routes[0].path == "/subscriptions"

    async def test_registered_handler_rejects_an_invalid_credential(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(), posture=Posture.OPTIONAL
        )
        router = add_realtime(
            app,
            Manifest(resources=[threads]),
            channel=InMemoryChannel(),
            dependency_builder=builder,
        )
        handler = router.routes[0].endpoint
        ws = _FakeWebSocket(headers={"X-API-Key": "nope"})
        await handler(ws)
        assert ws.closed_code == 1008
        assert ws.accepted is False

    async def test_registered_handler_accepts_an_anonymous_caller(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        router = add_realtime(app, Manifest(resources=[threads]), channel=InMemoryChannel())
        handler = router.routes[0].endpoint
        ws = _FakeWebSocket()
        task = asyncio.create_task(handler(ws))
        await _until(lambda: ws.accepted)
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)


# ---------------------------------------------------------------------------
# AsyncAPI document generation
# ---------------------------------------------------------------------------


class TestGenerateAsyncapiDocument:
    def test_includes_the_static_protocol_messages(self, threads: SqlResource[Any, Any]) -> None:
        document = generate_asyncapi_document(Manifest(resources=[threads]))
        assert document["asyncapi"] == "2.6.0"
        messages = document["components"]["messages"]
        for name in ("subscribe", "unsubscribe", "clientPing", "ack", "error", "serverPing"):
            assert name in messages

    def test_includes_an_event_message_for_a_readable_resource(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        document = generate_asyncapi_document(Manifest(resources=[threads]))
        assert "threadsEvent" in document["components"]["messages"]
        channel = document["channels"][DEFAULT_REALTIME_PATH]
        refs = {m["$ref"] for m in channel["subscribe"]["message"]["oneOf"]}
        assert "#/components/messages/threadsEvent" in refs

    def test_the_item_schema_is_the_resources_read_model(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        document = generate_asyncapi_document(Manifest(resources=[threads]))
        schemas = document["components"]["schemas"]
        assert "Threads" in schemas
        assert set(schemas["Threads"]["properties"]) == {"id", "title", "author_id"}

    def test_a_write_only_excluded_resource_contributes_no_event_message(self) -> None:
        from resourcey.core.service import Action
        from resourcey.view.resource_view import ResourceView

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner: SqlResource[Any, Any] = SqlResource(Thread, session_factory=maker)
        write_only = ResourceView(resource=inner, exposed_actions=frozenset({Action.CREATE}))
        document = generate_asyncapi_document(Manifest(resources=[write_only]))
        assert "threadsEvent" not in document["components"]["messages"]

    def test_document_is_json_serializable(self, threads: SqlResource[Any, Any]) -> None:
        document = generate_asyncapi_document(Manifest(resources=[threads]))
        json.dumps(document)  # must not raise

    def test_two_resources_do_not_collide_in_shared_schemas(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        thread_resource: SqlResource[Any, Any] = SqlResource(Thread, session_factory=maker)

        class OtherBase(DeclarativeBase):
            pass

        class Item(OtherBase):
            __tablename__ = "realtime_items"

            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
            title: Mapped[str] = mapped_column(String(100))

        item_resource: SqlResource[Any, Any] = SqlResource(Item, session_factory=maker)
        document = generate_asyncapi_document(Manifest(resources=[thread_resource, item_resource]))
        assert "Threads" in document["components"]["schemas"]
        assert "Items" in document["components"]["schemas"]

    def test_nested_defs_are_prefixed_and_refs_rewritten(self) -> None:
        """A field with a nested type (``$defs``, e.g. an enum) is renamed and re-pointed.

        Exercises the collision-avoidance machinery
        ``test_two_resources_do_not_collide_in_shared_schemas`` only sets up for:
        a plain scalar-only model has no ``$defs`` at all.
        """
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        resource: SqlResource[Any, Any] = SqlResource(Ticket, session_factory=maker)
        document = generate_asyncapi_document(Manifest(resources=[resource]))
        schemas = document["components"]["schemas"]
        assert "Tickets" in schemas
        assert "Tickets__Status" in schemas
        assert schemas["Tickets"]["properties"]["status"] == {
            "$ref": "#/components/schemas/Tickets__Status"
        }


class TestAddAsyncapi:
    async def test_mounted_document_is_served_as_json(self, threads: SqlResource[Any, Any]) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/asyncapi.json")
        assert response.status_code == 200
        document = response.json()
        assert document["asyncapi"] == "2.6.0"

    async def test_mounted_at_a_custom_path(self, threads: SqlResource[Any, Any]) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest, asyncapi_path="/docs/realtime.json")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/docs/realtime.json")
        assert response.status_code == 200

    async def test_html_viewer_is_served_at_the_default_path(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/asyncapi")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "AsyncApiStandalone.render" in response.text
        assert 'url: "/asyncapi.json"' in response.text

    async def test_html_viewer_is_mounted_at_a_custom_path(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest, html_path="/docs/realtime")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            default_response = await client.get("/asyncapi")
            custom_response = await client.get("/docs/realtime")
        assert default_response.status_code == 404
        assert custom_response.status_code == 200

    async def test_html_viewer_is_not_mounted_when_html_path_is_none(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest, html_path=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            viewer_response = await client.get("/asyncapi")
            json_response = await client.get("/asyncapi.json")
        assert viewer_response.status_code == 404
        assert json_response.status_code == 200

    async def test_neither_route_is_in_the_openapi_schema(
        self, threads: SqlResource[Any, Any]
    ) -> None:
        app = FastAPI()
        manifest = Manifest(resources=[threads])
        add_asyncapi(app, manifest)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/openapi.json")
        paths = response.json()["paths"]
        assert "/asyncapi.json" not in paths
        assert "/asyncapi" not in paths


class TestGetAsyncapiHtml:
    def test_points_at_the_given_json_url(self) -> None:
        response = get_asyncapi_html(asyncapi_url="/asyncapi.json", title="My API")
        assert response.status_code == 200
        body = bytes(response.body).decode()
        assert "My API" in body
        assert 'url: "/asyncapi.json"' in body
        assert "AsyncApiStandalone.render" in body

    def test_cdn_urls_are_overridable(self) -> None:
        response = get_asyncapi_html(
            asyncapi_url="/asyncapi.json",
            asyncapi_js_url="https://example.com/bundle.js",
            asyncapi_css_url="https://example.com/styles.css",
            asyncapi_favicon_url="https://example.com/favicon.ico",
        )
        body = bytes(response.body).decode()
        assert "https://example.com/bundle.js" in body
        assert "https://example.com/styles.css" in body
        assert "https://example.com/favicon.ico" in body
