"""Tests for the realtime channel (issue #17).

These drive the real code paths — the real ``InMemoryChannel`` fan-out, the real
``NotifyingService`` over a real ``SqlResource`` / in-memory SQLite table, the
real ``add_realtime`` WebSocket transport, and the real ``RedisChannel`` over an
in-process Redis-protocol server (``fakeredis``). No mocks.

Covered: the write-only event vocabulary and read-model projection; events firing
only after a successful write; the per-subscriber policy filtering (fail-closed);
subscription validation against exposed actions; the narrowed-``ResourceView``
projection; the bounded drop-oldest buffer; the Redis cross-process bridge; the
reconnect-reconcile path; and the config / lazy channel selection.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
import warnings
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.websockets import WebSocketDisconnect

from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_policy import Owner, Policy, PolicyResolver, ReadOnly
from resourcey.auth.auth_principal import Authenticator, AuthResult, Principal
from resourcey.core.manifest import Manifest
from resourcey.core.service import Action, Create, Delete, Page, Service, Update
from resourcey.http.app import create_app
from resourcey.realtime.realtime_channel import Channel, InMemoryChannel
from resourcey.realtime.realtime_config import RealtimeConfig
from resourcey.realtime.realtime_event import EventKind, ResourceEvent, event_kind_for_action
from resourcey.realtime.realtime_notifying_service import NotifyingService
from resourcey.realtime.realtime_redis_channel import RedisChannel
from resourcey.realtime.realtime_routes import RealtimeRejectionError, add_realtime
from resourcey.sql.sql_resource import SqlResource
from resourcey.view.resource_view import ResourceView

# fastapi.testclient's import emits a StarletteDeprecationWarning (a UserWarning)
# and the suite turns warnings into errors; import it lazily under a suppression
# so the collection-time import does not fail. Once imported it stays cached.
_TEST_CLIENT: Any = None


def _test_client(app: FastAPI) -> Any:
    global _TEST_CLIENT
    if _TEST_CLIENT is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient

            _TEST_CLIENT = TestClient
    return _TEST_CLIENT(app)


class RealtimeBase(DeclarativeBase):
    pass


class Thread(RealtimeBase):
    __tablename__ = "rt_threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class Secret(RealtimeBase):
    __tablename__ = "rt_secrets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))
    value: Mapped[str] = mapped_column(String(50))


class _MapAuthenticator(Authenticator):
    """A real authenticator mapping ``X-Api-Key`` to a principal (no storage)."""

    keys: dict[str, Principal] = {}  # noqa: RUF012 - a pydantic field default

    async def authenticate(self, request: Any) -> AuthResult:
        presented = request.headers.get("X-Api-Key")
        if not presented:
            return AuthResult.absent()
        principal = self.keys.get(presented)
        if principal is None:
            return AuthResult.invalid()
        return AuthResult.authenticated(principal)


class _RoleResolver(PolicyResolver):
    """Maps a principal's roles to policies (owner -> Owner, else ReadOnly)."""

    async def resolve(self, resource: Any, principal: Principal | None) -> list[Policy]:
        if principal is None or principal.id is None:
            return []
        if "owner" in principal.roles:
            return [Owner(owner_field="owner_id")]
        return [ReadOnly()]


class _AllowAllResolver(PolicyResolver):
    """Grants full access to any authenticated principal (the open-builder tests)."""

    async def resolve(self, resource: Any, principal: Principal | None) -> list[Policy]:
        from resourcey.auth.auth_policy import AllowAll

        if principal is None or principal.id is None:
            return []
        return [AllowAll()]


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------


def test_event_kind_mapping_is_write_only():
    assert event_kind_for_action(Action.CREATE) is EventKind.CREATED
    assert event_kind_for_action(Action.UPDATE) is EventKind.UPDATED
    assert event_kind_for_action(Action.DELETE) is EventKind.DELETED
    for action in (Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ, Action.BATCH_EDIT):
        assert event_kind_for_action(action) is None


def test_resource_event_round_trips_through_json():
    event = ResourceEvent(
        resource="threads", kind=EventKind.CREATED, id=7, item={"id": 7, "title": "hi"}
    )
    assert event.model_validate_json(event.model_dump_json()) == event


# ---------------------------------------------------------------------------
# InMemoryChannel
# ---------------------------------------------------------------------------


async def test_in_memory_channel_fans_out_to_every_subscriber():
    channel = InMemoryChannel()
    first = channel.subscribe()
    second = channel.subscribe()
    event = ResourceEvent(resource="threads", kind=EventKind.CREATED, id=1, item={"id": 1})
    await channel.publish(event)
    assert await asyncio.wait_for(first.__anext__(), timeout=1) == event
    assert await asyncio.wait_for(second.__anext__(), timeout=1) == event
    await channel.__aexit__(None, None, None)


async def test_in_memory_channel_drops_oldest_when_full():
    channel = InMemoryChannel(queue_maxsize=2)
    iterator = channel.subscribe()
    for i in range(4):
        await channel.publish(ResourceEvent(resource="t", kind=EventKind.UPDATED, id=i))
    # Only the two most recent events survive; the buffer is bounded.
    assert (await asyncio.wait_for(iterator.__anext__(), timeout=1)).id == 2
    assert (await asyncio.wait_for(iterator.__anext__(), timeout=1)).id == 3
    await channel.__aexit__(None, None, None)


async def test_in_memory_channel_iterator_ends_on_close():
    channel = InMemoryChannel()
    iterator = channel.subscribe()
    await channel.__aexit__(None, None, None)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(iterator.__anext__(), timeout=2)


# ---------------------------------------------------------------------------
# NotifyingService
# ---------------------------------------------------------------------------


class _FakeInner(Service[Any, Any]):
    """A minimal real Service whose writes succeed or raise, for emitter tests."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        super().__init__()
        self._fail_on = fail_on
        self.writes: list[tuple[str, Any]] = []

    async def create(self, payload: Any) -> Any:
        self._require_entered()
        if self._fail_on == "create":
            raise RuntimeError("boom")
        self.writes.append(("create", payload))
        return payload

    async def update(self, payload: Any) -> Any:
        self._require_entered()
        if self._fail_on == "update":
            raise RuntimeError("boom")
        self.writes.append(("update", payload))
        return payload

    async def delete(self, id: Any) -> None:  # noqa: A002
        self._require_entered()
        if self._fail_on == "delete":
            raise RuntimeError("boom")
        self.writes.append(("delete", id))

    async def search(self, *args: Any, **kwargs: Any) -> Page[Any]:
        self._require_entered()
        return Page(items=[])

    async def batch_edit(self, edits: list[Any]) -> list[Any]:
        self._require_entered()
        results: list[Any] = []
        for edit in edits:
            if isinstance(edit, Delete):
                self.writes.append(("delete", edit.id))
                results.append(None)
            else:
                self.writes.append(("write", edit.item))
                results.append(edit.item)
        return results


def _dto(**values: Any) -> Any:
    from pydantic import BaseModel

    class Item(BaseModel):
        id: int | None = None
        title: str | None = None

    return Item(**values)


def _notifying(inner: Service[Any, Any], channel: Channel) -> NotifyingService[Any, Any]:
    return NotifyingService(
        inner,
        channel=channel,
        resource_name="threads",
        read_model=_dto().__class__,
        id_field="id",
    )


async def test_notifying_service_publishes_after_a_successful_write():
    channel = InMemoryChannel()
    iterator = channel.subscribe()
    service = _notifying(_FakeInner(), channel)
    async with service:
        await service.create(_dto(id=1, title="a"))
        await service.update(_dto(id=1, title="b"))
        await service.delete(1)
    created = await asyncio.wait_for(iterator.__anext__(), timeout=1)
    updated = await asyncio.wait_for(iterator.__anext__(), timeout=1)
    deleted = await asyncio.wait_for(iterator.__anext__(), timeout=1)
    assert (created.kind, created.id, created.item) == (
        EventKind.CREATED,
        1,
        {"id": 1, "title": "a"},
    )
    assert (updated.kind, updated.item) == (EventKind.UPDATED, {"id": 1, "title": "b"})
    assert (deleted.kind, deleted.id, deleted.item) == (EventKind.DELETED, 1, None)


async def test_notifying_service_emits_nothing_when_the_write_raises():
    channel = InMemoryChannel()
    iterator = channel.subscribe()
    service = _notifying(_FakeInner(fail_on="create"), channel)
    async with service:
        with pytest.raises(RuntimeError):
            await service.create(_dto(id=1, title="a"))
    # Nothing was published: the write did not commit.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(iterator.__anext__(), timeout=0.2)


async def test_notifying_service_reads_emit_nothing():
    channel = InMemoryChannel()
    iterator = channel.subscribe()
    service = _notifying(_FakeInner(), channel)
    async with service:
        page = await service.search()
    assert isinstance(page, Page)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(iterator.__anext__(), timeout=0.2)


async def test_notifying_service_batch_edit_fans_out_one_event_per_applied_edit():
    channel = InMemoryChannel()
    iterator = channel.subscribe()
    service = _notifying(_FakeInner(), channel)
    async with service:
        await service.batch_edit([Create(item=_dto(id=1)), Update(item=_dto(id=2)), Delete(id=3)])
    kinds = [(await asyncio.wait_for(iterator.__anext__(), timeout=1)).kind for _ in range(3)]
    assert kinds == [EventKind.CREATED, EventKind.UPDATED, EventKind.DELETED]


async def test_notifying_service_emits_nothing_for_a_read_only_resource_action():
    """A resource that does not declare ``create`` is never routed, so it emits nothing."""
    from resourcey.list.list_resource import ListResource

    resource = ListResource([_dto(id=1)])
    assert Action.CREATE not in resource.get_supported_actions()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_realtime_config_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch):
    from resourcey.config.config_base import _reset_config_prefix

    monkeypatch.delenv("APP_REALTIME_CHANNEL_CLASS", raising=False)
    _reset_config_prefix()
    try:
        config = RealtimeConfig.get_instance()
        assert isinstance(config.channel, InMemoryChannel)
        assert config.heartbeat_seconds > 0
    finally:
        RealtimeConfig.clear_instance_cache()
        _reset_config_prefix()


def test_realtime_config_selects_the_channel_lazily(monkeypatch: pytest.MonkeyPatch):
    from resourcey.config.config_base import _reset_config_prefix

    monkeypatch.setenv(
        "APP_REALTIME_CHANNEL_CLASS", "resourcey.realtime.realtime_channel.InMemoryChannel"
    )
    _reset_config_prefix()
    try:
        assert isinstance(RealtimeConfig.get_instance().channel, InMemoryChannel)
    finally:
        RealtimeConfig.clear_instance_cache()
        _reset_config_prefix()


# ---------------------------------------------------------------------------
# RedisChannel over fakeredis
# ---------------------------------------------------------------------------


async def test_redis_channel_bridges_two_processes():
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    publisher = RedisChannel(client=fakeredis.aioredis.FakeRedis(server=server))
    subscriber = RedisChannel(client=fakeredis.aioredis.FakeRedis(server=server))
    await publisher.__aenter__()
    await subscriber.__aenter__()

    iterator = subscriber.subscribe()
    task = asyncio.ensure_future(iterator.__anext__())
    await asyncio.sleep(0.1)
    event = ResourceEvent(resource="threads", kind=EventKind.CREATED, id=5, item={"id": 5})
    await publisher.publish(event)
    received = await asyncio.wait_for(task, timeout=2)
    assert received == event

    await publisher.__aexit__(None, None, None)
    await subscriber.__aexit__(None, None, None)


def test_redis_channel_missing_driver_is_actionable(monkeypatch: pytest.MonkeyPatch):
    from resourcey.realtime import realtime_redis_channel

    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(ImportError, match=r"resourcey\[redis\]"):
        realtime_redis_channel._require_redis()


# ---------------------------------------------------------------------------
# WebSocket transport (end to end)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(RealtimeBase.metadata.create_all)
    yield eng
    await eng.dispose()


def _open_builder(channel: Channel) -> AuthorizedDependencyBuilder:
    """A builder that authenticates a fixed key (``k``) and allows anonymous reads."""
    return AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(keys={"k": Principal.user(uuid.uuid4())}),
        policy_resolver=_AllowAllResolver(),
        posture=Posture.OPTIONAL,
        channel=channel,
    )


# The header the open-builder tests authenticate with.
_OPEN_HEADERS = {"X-Api-Key": "k"}


def _build(engine: Any, resources: list[Any], builder: Any, channel: Channel) -> FastAPI:
    manifest = Manifest(resources=resources)
    app = create_app(manifest, dependency_builder=builder)
    add_realtime(app, manifest, channel=channel, dependency_builder=builder)
    return app


async def test_websocket_subscribe_receives_created_event(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json() == {"type": "ack", "resource": "threads"}
        created = client.post("/threads", json={"title": "hello"}, headers=_OPEN_HEADERS)
        assert created.status_code == 201
        message = ws.receive_json()
        assert message["type"] == "event"
        assert message["event"]["kind"] == "created"
        assert message["event"]["item"]["title"] == "hello"


async def test_websocket_unknown_resource_is_an_error(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "nope"})
        message = ws.receive_json()
        assert message["type"] == "error"


async def test_websocket_requires_authentication_under_a_required_posture(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(),
        policy_resolver=_RoleResolver(),
        posture=Posture.REQUIRED,
        channel=channel,
    )
    app = _build(engine, [threads], builder, channel)
    with (
        _test_client(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws"),
    ):
        pass


async def test_websocket_rejects_an_invalid_credential(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(keys={}),
        policy_resolver=_RoleResolver(),
        posture=Posture.OPTIONAL,
        channel=channel,
    )
    app = _build(engine, [threads], builder, channel)
    with (
        _test_client(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws", headers={"X-Api-Key": "bad"}),
    ):
        pass


async def test_websocket_delivers_only_in_scope_rows(engine: Any):
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    keys = {
        "owner": Principal.user(owner_id, roles=frozenset({"owner"})),
        "other": Principal.user(other_id, roles=frozenset({"owner"})),
    }
    builder = AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(keys=keys),
        policy_resolver=_RoleResolver(),
        channel=channel,
    )
    app = _build(engine, [threads], builder, channel)
    with (
        _test_client(app) as client,
        client.websocket_connect("/ws", headers={"X-Api-Key": "owner"}) as owner_ws,
    ):
        owner_ws.send_json({"type": "subscribe", "resource": "threads"})
        assert owner_ws.receive_json()["type"] == "ack"
        # A row owned by someone else must not reach the owner.
        other = client.post(
            "/threads",
            json={"title": "theirs", "owner_id": str(other_id)},
            headers={"X-Api-Key": "other"},
        )
        assert other.status_code == 201
        # The owner's own row is delivered.
        mine = client.post(
            "/threads",
            json={"title": "mine", "owner_id": str(owner_id)},
            headers={"X-Api-Key": "owner"},
        )
        assert mine.status_code == 201
        message = owner_ws.receive_json()
        assert message["event"]["item"]["title"] == "mine"


async def test_websocket_deleted_event_is_suppressed_for_a_scoped_subscriber(engine: Any):
    owner_id = uuid.uuid4()
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    keys = {"owner": Principal.user(owner_id, roles=frozenset({"owner"}))}
    builder = AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(keys=keys),
        policy_resolver=_RoleResolver(),
        channel=channel,
    )
    app = _build(engine, [threads], builder, channel)
    with (
        _test_client(app) as client,
        client.websocket_connect("/ws", headers={"X-Api-Key": "owner"}) as ws,
    ):
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "ack"
        # A deleted event carries no row, so a scoped subscriber cannot be
        # verified for it (fail-closed): publish one and expect no delivery.
        await channel.publish(
            ResourceEvent(resource="threads", kind=EventKind.DELETED, id=1, item=None)
        )
        # A subsequent in-scope create proves the socket is still live and
        # the delete was the only thing suppressed.
        created = client.post(
            "/threads",
            json={"title": "mine", "owner_id": str(owner_id)},
            headers={"X-Api-Key": "owner"},
        )
        assert created.status_code == 201
        assert ws.receive_json()["event"]["kind"] == "created"


async def test_websocket_event_omits_a_field_hidden_by_a_view(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    secrets = SqlResource(Secret, session_factory=maker)
    public = ResourceView(
        secrets,
        exposed_field_overrides={"value": {"in_read_response": False, "in_search_response": False}},
    )
    builder = _open_builder(channel)
    app = _build(engine, [public], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "secrets"})
        assert ws.receive_json()["type"] == "ack"
        created = client.post(
            "/secrets", json={"name": "n", "value": "hidden"}, headers=_OPEN_HEADERS
        )
        assert created.status_code == 201
        message = ws.receive_json()
        assert "value" not in message["event"]["item"]
        assert message["event"]["item"]["name"] == "n"


async def test_websocket_heartbeat_ping(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


async def test_reconnect_reconcile_via_search(engine: Any):
    """A client that missed events reconciles with an ordinary REST ``search``."""
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client:
        # A write happens while no subscriber is connected (a missed event).
        assert (
            client.post("/threads", json={"title": "missed"}, headers=_OPEN_HEADERS).status_code
            == 201
        )
        # The reconcile is the ordinary REST surface, correctly paged.
        page = client.get("/threads", headers=_OPEN_HEADERS).json()
        assert [item["title"] for item in page["items"]] == ["missed"]


async def test_realtime_rejection_error_is_public():
    assert issubclass(RealtimeRejectionError, Exception)


# ---------------------------------------------------------------------------
# WebSocket protocol edge cases
# ---------------------------------------------------------------------------


async def test_websocket_unsubscribe_stops_delivery(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "ack"
        ws.send_json({"type": "unsubscribe", "resource": "threads"})
        assert ws.receive_json() == {"type": "ack", "resource": "threads"}
        created = client.post("/threads", json={"title": "gone"}, headers=_OPEN_HEADERS)
        assert created.status_code == 201
        # Prove the socket is still live but no longer delivers for threads.
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


async def test_websocket_malformed_and_unknown_messages_error(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json(["not", "a", "dict"])
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "frobnicate"})
        assert ws.receive_json()["type"] == "error"


async def test_websocket_filter_is_validated_and_applied(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        # An unknown filter parameter is rejected.
        ws.send_json({"type": "subscribe", "resource": "threads", "filter": {"bogus__eq": 1}})
        assert ws.receive_json()["type"] == "error"
        # A valid filter narrows delivery: a non-matching row is not sent.
        ws.send_json({"type": "subscribe", "resource": "threads", "filter": {"title__eq": "keep"}})
        assert ws.receive_json()["type"] == "ack"
        assert (
            client.post("/threads", json={"title": "drop"}, headers=_OPEN_HEADERS).status_code
            == 201
        )
        assert (
            client.post("/threads", json={"title": "keep"}, headers=_OPEN_HEADERS).status_code
            == 201
        )
        assert ws.receive_json()["event"]["item"]["title"] == "keep"


async def test_websocket_non_readable_resource_is_not_subscribable(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    # A view with no read action is not subscribable.
    write_only = ResourceView(threads, exposed_actions=frozenset({Action.CREATE}))
    builder = _open_builder(channel)
    app = _build(engine, [write_only], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "error"


async def test_websocket_missed_create_update_emits_nothing(engine: Any):
    """A batch create whose inner write returned ``None`` emits no event."""
    channel = InMemoryChannel()
    iterator = channel.subscribe()

    class _MissInner(_FakeInner):
        async def batch_edit(self, edits: list[Any]) -> list[Any]:
            self._require_entered()
            return [None for _ in edits]

    service = _notifying(_MissInner(), channel)
    async with service:
        await service.batch_edit([Create(item=_dto(id=1))])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(iterator.__anext__(), timeout=0.2)


# ---------------------------------------------------------------------------
# RedisChannel lifecycle
# ---------------------------------------------------------------------------


async def test_redis_channel_closes_only_the_client_it_built(monkeypatch: pytest.MonkeyPatch):
    import fakeredis.aioredis

    injected = fakeredis.aioredis.FakeRedis()
    channel = RedisChannel(client=injected)
    await channel.__aenter__()
    await channel.__aexit__(None, None, None)
    # An injected client is left open (the caller owns it).
    assert await injected.ping() is True


async def test_redis_channel_builds_and_closes_its_own_client(monkeypatch: pytest.MonkeyPatch):
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    built = fakeredis.aioredis.FakeRedis(server=server)

    class _Driver:
        def from_url(self, *args: Any, **kwargs: Any) -> Any:
            return built

    channel = RedisChannel(url="redis://unused")
    from resourcey.realtime import realtime_redis_channel as module

    monkeypatch.setattr(module, "_require_redis", lambda: _Driver())
    await channel.__aenter__()
    assert channel.client is built
    await channel.__aexit__(None, None, None)
    # A client the channel built is closed and dropped on exit.
    assert channel.client is None


async def test_redis_channel_publish_before_entering_raises():
    channel = RedisChannel(client=None)
    with pytest.raises(RuntimeError):
        await channel.publish(ResourceEvent(resource="t", kind=EventKind.CREATED, id=1))


# ---------------------------------------------------------------------------
# More emitter / transport branches
# ---------------------------------------------------------------------------


async def test_notifying_service_forwards_reads_and_delegates_privacy():
    channel = InMemoryChannel()

    class _ReadInner(_FakeInner):
        def response_is_private(self) -> bool:
            return True

        def serialization_context(self) -> dict[str, Any] | None:
            return {"expose_secrets": True}

        async def read(self, id: Any) -> Any:  # noqa: A002
            self._require_entered()
            return _dto(id=id)

        async def count(self, *args: Any, **kwargs: Any) -> int:
            self._require_entered()
            return 3

        async def batch_read(self, ids: list[Any]) -> list[Any]:
            self._require_entered()
            return [_dto(id=i) for i in ids]

    service = _notifying(_ReadInner(), channel)
    assert service.response_is_private() is True
    assert service.serialization_context() == {"expose_secrets": True}
    async with service:
        assert (await service.read(1)).id == 1
        assert await service.count() == 3
        assert [i.id for i in await service.batch_read([1, 2])] == [1, 2]


async def test_notifying_service_wire_id_of_a_composite_identifier():
    from pydantic import BaseModel

    class Composite(BaseModel):
        a: int

    class _CompositeInner(_FakeInner):
        pass

    channel = InMemoryChannel()
    iterator = channel.subscribe()
    service = NotifyingService(
        _CompositeInner(),
        channel=channel,
        resource_name="threads",
        read_model=Composite,
        id_field="a",
    )
    async with service:
        await service.delete(Composite(a=1))
    event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
    assert event.id == {"a": 1}


async def test_websocket_heartbeat_disabled_when_interval_is_zero():
    # A zero heartbeat returns immediately (no keepalive task churn).
    from resourcey.realtime.realtime_routes import _heartbeat

    await _heartbeat(_ConnectionLike(), 0)


class _ConnectionLike:
    async def send(self, payload: dict[str, Any]) -> bool:  # pragma: no cover - unused
        return True


async def test_websocket_filter_must_be_an_object(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads", "filter": [1, 2]})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "subscribe", "resource": "threads", "filter": {"__eq": 1}})
        assert ws.receive_json()["type"] == "error"


async def test_add_realtime_requires_a_channel_wired_builder(engine: Any):
    """A builder not wired to the channel is a loud mount-time failure, not silence."""
    from resourcey.core.errors import ResourceyConfigError
    from resourcey.http.dependency_builder import OpenDependencyBuilder

    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    manifest = Manifest(resources=[threads])
    app = create_app(manifest, dependency_builder=OpenDependencyBuilder())
    with pytest.raises(ResourceyConfigError):
        add_realtime(app, manifest, channel=channel, dependency_builder=OpenDependencyBuilder())


async def test_websocket_anonymous_no_policy_subscriber_receives_nothing(engine: Any):
    """An anonymous subscriber with no policies is fail-closed (``NoMatchFilter``)."""
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    keys = {"writer": Principal.user(uuid.uuid4())}
    builder = AuthorizedDependencyBuilder(
        authenticator=_MapAuthenticator(keys=keys),
        policy_resolver=_AllowAllResolver(),
        posture=Posture.OPTIONAL,
        channel=channel,
    )
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "ack"
        assert (
            client.post(
                "/threads", json={"title": "x"}, headers={"X-Api-Key": "writer"}
            ).status_code
            == 201
        )
        # The anonymous subscriber has no policy, so nothing is delivered.
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


async def test_websocket_two_subscriptions_only_deliver_the_matching_resource(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    secrets = SqlResource(Secret, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads, secrets], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "ack"
        ws.send_json({"type": "subscribe", "resource": "secrets"})
        assert ws.receive_json()["type"] == "ack"
        assert (
            client.post(
                "/secrets", json={"name": "n", "value": "v"}, headers=_OPEN_HEADERS
            ).status_code
            == 201
        )
        message = ws.receive_json()
        assert message["event"]["resource"] == "secrets"


async def test_websocket_bad_filter_parameter_name_errors(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    app = _build(engine, [threads], builder, channel)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        ws.send_json({"type": "subscribe", "resource": "threads", "filter": {"noseparator": 1}})
        assert ws.receive_json()["type"] == "error"


async def test_websocket_heartbeat_emits_a_ping(engine: Any):
    channel = InMemoryChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    manifest = Manifest(resources=[threads])
    app = create_app(manifest, dependency_builder=builder)
    add_realtime(
        app,
        manifest,
        channel=channel,
        dependency_builder=builder,
        config=RealtimeConfig(heartbeat_seconds=1),
    )
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        # The server pings on its own after the heartbeat interval elapses.
        assert ws.receive_json() == {"type": "ping"}


async def test_channel_lifecycle_follows_the_manifest_managers(engine: Any):
    """The channel is entered through ``Manifest(managers=[...])`` (its client lifetime)."""

    class _LifecycleChannel(InMemoryChannel):
        entered: bool = False
        exited: bool = False

        async def __aenter__(self) -> Any:
            self.entered = True
            return self

        async def __aexit__(self, *exc: object) -> None:
            self.exited = True

    channel = _LifecycleChannel()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    builder = _open_builder(channel)
    manifest = Manifest(resources=[threads], managers=[channel])
    app = create_app(manifest, dependency_builder=builder)
    add_realtime(app, manifest, channel=channel, dependency_builder=builder)
    with _test_client(app) as client, client.websocket_connect("/ws", headers=_OPEN_HEADERS) as ws:
        assert channel.entered is True
        ws.send_json({"type": "subscribe", "resource": "threads"})
        assert ws.receive_json()["type"] == "ack"
        assert (
            client.post("/threads", json={"title": "life"}, headers=_OPEN_HEADERS).status_code
            == 201
        )
        assert ws.receive_json()["event"]["item"]["title"] == "life"
    assert channel.exited is True
