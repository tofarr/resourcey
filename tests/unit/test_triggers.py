"""Tests for the edit-event trigger framework (issue #155).

Exercised against the **real** production code paths (no mocks): a real
``SqlResource`` / ``SqlService`` over an in-memory SQLite database, real
``Trigger`` subclasses, and the real ``TriggeredResource`` / ``TriggeredService``
/ ``TriggeredDependencyBuilder``. Covered, by layer:

* ``Trigger`` construction via ``on_edit`` — bare-callable and duplicate
  rejection.
* ``TriggeredResource`` — every surface-defining method delegates verbatim
  (schema, actions, query / sort surface, cache policy, registration,
  lifecycle), and ``get_exposed_resource()`` returns the wrapper itself.
* ``TriggeredService`` firing — edits-only, success-only, per-operation (not
  per-item) with aligned ``(edits, results)``, per-trigger isolation,
  background vs. inline execution, and in-flight cancellation on exit.
* ``TriggerConfig`` — grouping by resource path, and genuine env parsing.
* ``TriggeredDependencyBuilder`` — per-path wrapping, no-op passthrough,
  composition with an inner builder, and no double-wrap of a
  ``TriggeredResource``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from pydantic import Field, PrivateAttr
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.errors import ResourceyConfigError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import (
    Create,
    Delete,
    NotFoundError,
    Service,
    ServiceError,
    Update,
)
from resourcey.http.app import create_app
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.trigger_config import TriggerConfig, TriggerEntry
from resourcey.triggers.trigger_runner import TriggerRunner
from resourcey.triggers.triggered_dependency_builder import TriggeredDependencyBuilder
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.triggers.triggered_service import TriggeredService
from resourcey.util.models import clear_subclass_cache


class TriggersBase(DeclarativeBase):
    pass


class Thread(TriggersBase):
    __tablename__ = "trigger_threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))


# ---------------------------------------------------------------------------
# Test doubles (real Trigger subclasses — no mocking)
# ---------------------------------------------------------------------------


class RecordingTrigger(Trigger):
    """A trigger that records every ``(edits, results)`` pair it was called with."""

    label: str = "default"
    _calls: list[tuple[list[Any], list[Any]]] = PrivateAttr(default_factory=list)

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        self._calls.append((list(edits), list(results)))

    @property
    def calls(self) -> list[tuple[list[Any], list[Any]]]:
        return self._calls


class RaisingTrigger(Trigger):
    """A trigger whose callback always raises, to prove isolation."""

    label: str = "boom"
    _calls: int = PrivateAttr(default=0)

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        self._calls += 1
        raise RuntimeError("boom")


class SlowTrigger(Trigger):
    """A trigger that sleeps indefinitely, to prove background cancellation on exit."""

    label: str = "slow"
    _started: bool = PrivateAttr(default=False)
    _cancelled: bool = PrivateAttr(default=False)

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        self._started = True
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self._cancelled = True
            raise


class CancellingTrigger(Trigger):
    """A trigger whose callback raises ``CancelledError`` directly (not via cancellation)."""

    label: str = "cancelling"

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        raise asyncio.CancelledError


class GatedTrigger(Trigger):
    """A trigger whose callback blocks until released, to prove background non-blocking.

    All ``Trigger`` subclasses must be module-level: the discriminated-union
    machinery needs to resolve a concrete class by name later, which a local
    (function-scoped) class cannot support.
    """

    label: str = "gated"
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _released: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        self._started.set()
        await self._released.wait()

    @property
    def started(self) -> bool:
        return self._started.is_set()

    def release(self) -> None:
        self._released.set()


def _maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_async_engine("sqlite+aiosqlite:///:memory:"), expire_on_commit=False
    )


@pytest_asyncio.fixture
async def inner() -> AsyncIterator[SqlResource[Any, Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource: SqlResource[Any, Any] = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(TriggersBase.metadata.create_all)
    yield resource
    await engine.dispose()


# ---------------------------------------------------------------------------
# TriggerRunner — the shared, app-scoped firing/tracking primitive
# ---------------------------------------------------------------------------


class TestTriggerRunner:
    async def test_no_triggers_is_a_no_op(self) -> None:
        runner = TriggerRunner()
        await runner.fire([], [], [], background=True)
        assert runner.in_flight == frozenset()

    async def test_inline_awaits_before_returning(self) -> None:
        trigger = RecordingTrigger()
        runner = TriggerRunner()
        await runner.fire([trigger], [Create(item="x")], ["x"], background=False)
        assert len(trigger.calls) == 1
        assert runner.in_flight == frozenset()

    async def test_background_tracks_then_self_discards_on_completion(self) -> None:
        trigger = RecordingTrigger()
        runner = TriggerRunner()
        await runner.fire([trigger], [Create(item="x")], ["x"], background=True)
        assert len(runner.in_flight) == 1
        assert trigger.calls == []  # scheduled, not yet run
        # A handful of hops: one schedules the tracked task, the next
        # schedules ``asyncio.gather``'s own child task for the trigger's
        # callback, and a couple more let the finished tracked task's
        # done_callback actually run and discard it.
        for _ in range(5):
            await asyncio.sleep(0)
        assert trigger.calls == [([Create(item="x")], ["x"])]
        assert runner.in_flight == frozenset()  # done_callback discarded it

    async def test_aclose_cancels_and_awaits_pending_runs(self) -> None:
        trigger = SlowTrigger()
        runner = TriggerRunner()
        await runner.fire([trigger], [], [], background=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let it actually start sleeping
        assert len(runner.in_flight) == 1
        await runner.aclose()
        assert runner.in_flight == frozenset()
        assert trigger._cancelled is True

    async def test_aclose_is_idempotent(self) -> None:
        runner = TriggerRunner()
        await runner.aclose()
        await runner.aclose()
        assert runner.in_flight == frozenset()


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_bare_callable_is_rejected(self, inner: SqlResource[Any, Any]) -> None:
        async def plain(edits: Any, results: Any) -> None:
            return None

        with pytest.raises(ResourceyConfigError, match="Trigger instances"):
            TriggeredResource(inner, on_edit=[plain])  # type: ignore[list-item]

    def test_duplicate_trigger_is_rejected(self, inner: SqlResource[Any, Any]) -> None:
        with pytest.raises(ResourceyConfigError, match="Duplicate"):
            TriggeredResource(
                inner, on_edit=[RecordingTrigger(label="a"), RecordingTrigger(label="a")]
            )

    def test_distinct_triggers_are_accepted(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(
            inner, on_edit=[RecordingTrigger(label="a"), RecordingTrigger(label="b")]
        )
        assert wrapped.get_resource_path() == inner.get_resource_path()

    def test_no_triggers_is_accepted(self, inner: SqlResource[Any, Any]) -> None:
        TriggeredResource(inner)


# ---------------------------------------------------------------------------
# Delegation — every surface-defining method is the inner's
# ---------------------------------------------------------------------------


class TestDelegation:
    def test_schema_and_surface_match_the_inner(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(inner, on_edit=[RecordingTrigger()])
        assert wrapped.get_dto_type() is inner.get_dto_type()
        assert wrapped.get_rest_models() == inner.get_rest_models()
        assert wrapped.get_id_field() == inner.get_id_field()
        assert wrapped.get_resource_path() == inner.get_resource_path()
        assert wrapped.get_cache_strategy() == inner.get_cache_strategy()
        assert wrapped.get_queryable_fields() == inner.get_queryable_fields()
        assert wrapped.get_filter_operators() == inner.get_filter_operators()
        assert wrapped.get_search_filter_type() == inner.get_search_filter_type()
        assert wrapped.get_sortable_fields() == inner.get_sortable_fields()
        assert wrapped.get_sort_order_type() == inner.get_sort_order_type()
        assert wrapped.get_supported_actions() == inner.get_supported_actions()
        assert wrapped.resolve_sort_order("id", False) == inner.resolve_sort_order("id", False)

    def test_get_exposed_resource_is_the_wrapper(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(inner, on_edit=[RecordingTrigger()])
        assert wrapped.get_exposed_resource() is wrapped

    def test_registration_forwards_to_the_inner(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(inner, on_edit=[RecordingTrigger()])
        manifest = Manifest(resources=[wrapped])
        assert wrapped.get_manifest() is manifest
        assert inner.get_manifest() is manifest

    async def test_lifecycle_delegates_to_the_inner(self, inner: SqlResource[Any, Any]) -> None:
        entered: list[str] = []

        class Recording(SqlResource[Any, Any]):
            async def __aenter__(self) -> Any:
                entered.append("enter")
                return await super().__aenter__()

            async def __aexit__(self, *exc: object) -> None:
                entered.append("exit")
                await super().__aexit__(*exc)

        recording = Recording(Thread, session_factory=_maker())
        wrapped = TriggeredResource(recording, on_edit=[RecordingTrigger()])
        async with wrapped:
            assert entered == ["enter"]
        assert entered == ["enter", "exit"]

    async def test_double_entry_raises(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(inner)
        async with wrapped:
            with pytest.raises(ServiceError, match="already entered"):
                await wrapped.__aenter__()


# ---------------------------------------------------------------------------
# Firing — edits-only, success-only, per-operation alignment, isolation
# ---------------------------------------------------------------------------


class TestFiring:
    async def test_create_fires_once_aligned(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            created = await service.create(dto_type.model_validate({"title": "hello"}))
        assert len(trigger.calls) == 1
        edits, results = trigger.calls[0]
        assert len(edits) == 1 and len(results) == 1
        assert isinstance(edits[0], Create)
        assert edits[0].item.title == "hello"
        assert results[0].id == created.id

    async def test_update_fires_once_aligned(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            created = await service.create(dto_type.model_validate({"title": "hello"}))
            updated = await service.update(
                dto_type.model_validate({"id": created.id, "title": "updated"})
            )
        assert len(trigger.calls) == 2  # one create, one update
        edits, results = trigger.calls[1]
        assert isinstance(edits[0], Update)
        assert results[0].title == "updated"
        assert updated.title == "updated"

    async def test_delete_fires_once_with_none_result(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            created = await service.create(dto_type.model_validate({"title": "hello"}))
            await service.delete(created.id)
        edits, results = trigger.calls[-1]
        assert isinstance(edits[0], Delete)
        assert edits[0].id == created.id
        assert results == [None]

    async def test_batch_edit_fires_once_not_per_item(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            batch: list[Create[Any] | Update[Any] | Delete[Any]] = [
                Create(item=dto_type.model_validate({"title": "a"})),
                Create(item=dto_type.model_validate({"title": "b"})),
                Create(item=dto_type.model_validate({"title": "c"})),
            ]
            await service.batch_edit(batch)
        # One invocation for the whole operation, carrying all three edits,
        # not three separate invocations.
        assert len(trigger.calls) == 1
        edits, results = trigger.calls[0]
        assert len(edits) == 3
        assert len(results) == 3

    async def test_reads_never_fire(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            created = await service.create(dto_type.model_validate({"title": "hello"}))
            assert len(trigger.calls) == 1
            await service.read(created.id)
            await service.search()
            await service.count()
            await service.batch_read([created.id])
            # None of the read-like actions added an invocation.
            assert len(trigger.calls) == 1

    async def test_failed_edit_fires_nothing(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            with pytest.raises(NotFoundError):
                await service.update(dto_type.model_validate({"id": 99999, "title": "nope"}))
            with pytest.raises(NotFoundError):
                await service.delete(99999)
        assert trigger.calls == []

    async def test_per_trigger_isolation(
        self, inner: SqlResource[Any, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        good = RecordingTrigger(label="good")
        bad = RaisingTrigger()
        wrapped = TriggeredResource(inner, on_edit=[good, bad], background=False)
        with caplog.at_level(logging.ERROR):
            async with await wrapped.get_service() as service:
                dto_type = wrapped.get_dto_type()
                await service.create(dto_type.model_validate({"title": "hello"}))
        # The raising trigger ran (and was isolated)...
        assert bad._calls == 1
        # ...and the other trigger still ran, unaffected.
        assert len(good.calls) == 1
        assert "RaisingTrigger" in caplog.text
        assert "boom" in caplog.text

    async def test_background_does_not_block_the_write(self, inner: SqlResource[Any, Any]) -> None:
        trigger = GatedTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=True)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            # The write returns even though the trigger has not run to
            # completion (it is gated on ``release()``).
            await service.create(dto_type.model_validate({"title": "hello"}))
            assert not trigger.started
            # Two hops: one schedules the tracked task, the next schedules
            # ``asyncio.gather``'s own child task for the trigger's callback.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert trigger.started
            trigger.release()
            await asyncio.sleep(0)  # let the background run finish cleanly

    async def test_background_survives_the_per_request_service_exit(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        """A per-request service's own exit must not cancel a background run.

        ``TriggeredService`` is built fresh **per request** (``get_service()``
        is awaited once per call); tracking -- and cancelling -- an in-flight
        background run on *that* instance would settle it before the event
        loop ever gives it a turn to execute, before any real webhook latency
        could elapse (see ``trigger_runner.py``). The resource's one, shared
        ``TriggerRunner`` tracks it instead, so it survives this single
        request's service exiting.
        """
        trigger = SlowTrigger()
        wrapped = TriggeredResource(inner, on_edit=[trigger], background=True)
        async with wrapped:
            service = await wrapped.get_service()
            assert isinstance(service, TriggeredService)
            async with service:
                dto_type = wrapped.get_dto_type()
                await service.create(dto_type.model_validate({"title": "hello"}))
                await asyncio.sleep(0)
                await asyncio.sleep(0)  # let the background task actually start sleeping
                assert len(service.runner.in_flight) == 1
            # The per-request service exited -- the run is untouched.
            assert trigger._started is True
            assert trigger._cancelled is False
            assert len(service.runner.in_flight) == 1
        # The *resource* exited (the app-scoped owner) -- that drains it.
        assert trigger._cancelled is True
        assert service.runner.in_flight == frozenset()

    async def test_no_configured_triggers_is_a_no_op(self, inner: SqlResource[Any, Any]) -> None:
        wrapped = TriggeredResource(inner, background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            created = await service.create(dto_type.model_validate({"title": "hello"}))
        assert created.title == "hello"

    async def test_a_trigger_raising_cancelled_error_reraises(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        wrapped = TriggeredResource(inner, on_edit=[CancellingTrigger()], background=False)
        async with await wrapped.get_service() as service:
            dto_type = wrapped.get_dto_type()
            with pytest.raises(asyncio.CancelledError):
                await service.create(dto_type.model_validate({"title": "hello"}))

    async def test_a_service_used_before_entering_raises(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        wrapped = TriggeredResource(inner, on_edit=[RecordingTrigger()])
        service = await wrapped.get_service()
        with pytest.raises(ServiceError, match="before entering it"):
            await service.count()

    async def test_serialization_context_and_privacy_delegate(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        wrapped = TriggeredResource(inner, on_edit=[RecordingTrigger()], background=False)
        async with await wrapped.get_service() as service:
            assert (
                service.serialization_context()
                == (await inner.get_service()).serialization_context()
            )
            assert service.response_is_private() is False


# ---------------------------------------------------------------------------
# TriggerConfig — grouping and genuine env parsing
# ---------------------------------------------------------------------------


class _WebhookLikeTrigger(Trigger):
    """A module-level trigger kind so its dotted path is importable from env."""

    url: str = "https://example.com/hook"

    async def callback(self, edits: list[Any], results: list[Any]) -> None:
        return None


class TestTriggerConfig:
    def test_resource_triggers_groups_by_path(self) -> None:
        a = _WebhookLikeTrigger(url="https://a")
        b = _WebhookLikeTrigger(url="https://b")
        c = _WebhookLikeTrigger(url="https://c")
        config = TriggerConfig(
            triggers=[
                TriggerEntry(resource_path="threads", trigger=a),
                TriggerEntry(resource_path="threads", trigger=b),
                TriggerEntry(resource_path="messages", trigger=c),
            ]
        )
        grouped = config.resource_triggers()
        assert grouped["threads"] == [a, b]
        assert grouped["messages"] == [c]
        assert "unconfigured" not in grouped

    def test_empty_config_groups_to_nothing(self) -> None:
        assert TriggerConfig().resource_triggers() == {}

    def test_genuine_env_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from resourcey.util.env_parser import from_env

        monkeypatch.setenv("APP_TRIGGERS_0_RESOURCE_PATH", "threads")
        monkeypatch.setenv(
            "APP_TRIGGERS_0_TRIGGER_KIND",
            "tests.unit.test_triggers._WebhookLikeTrigger",
        )
        monkeypatch.setenv("APP_TRIGGERS_0_TRIGGER_URL", "https://example.com/hook")
        clear_subclass_cache()
        config = from_env(TriggerConfig, prefix="APP")
        assert len(config.triggers) == 1
        entry = config.triggers[0]
        assert entry.resource_path == "threads"
        assert isinstance(entry.trigger, _WebhookLikeTrigger)
        assert entry.trigger.url == "https://example.com/hook"


# ---------------------------------------------------------------------------
# TriggeredDependencyBuilder
# ---------------------------------------------------------------------------


class _RecordingInnerBuilder(DependencyBuilder):
    """A builder recording whether its dependency was reached, for composition tests.

    ``get_service_dependency`` memoizes by resource identity so a test can
    compare the *exact* dependency object a builder yields — the identity
    ``TriggeredDependencyBuilder`` passes through verbatim when a resource has
    no configured triggers.
    """

    marker: list[str] = Field(default_factory=list)
    _cache: dict[int, Any] = PrivateAttr(default_factory=dict)

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Any:
        key = id(resource)
        if key not in self._cache:
            marker = self.marker

            async def dependency(request: Request) -> AsyncIterator[Service[Any, Any]]:
                marker.append("reached")
                service = await resource.get_service()
                async with service:
                    yield service

            self._cache[key] = dependency
        return self._cache[key]

    def get_principal_dependency(self) -> Any:
        async def principal() -> str:
            return "recorded"

        return principal


class TestTriggeredDependencyBuilder:
    def test_passthrough_when_no_triggers_configured(self, inner: SqlResource[Any, Any]) -> None:
        recorded = _RecordingInnerBuilder()
        builder = TriggeredDependencyBuilder(inner=recorded)
        expected = recorded.get_service_dependency(inner)
        # No entry for this path -> the inner builder's dependency object is
        # returned verbatim, not wrapped.
        assert builder.get_service_dependency(inner) is expected

    def test_wraps_the_configured_path_in_a_new_dependency(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        recorded = _RecordingInnerBuilder()
        trigger = RecordingTrigger()
        builder = TriggeredDependencyBuilder(
            inner=recorded,
            resource_triggers={inner.get_resource_path(): [trigger]},
        )
        inner_dependency = recorded.get_service_dependency(inner)
        wrapping_dependency = builder.get_service_dependency(inner)
        assert wrapping_dependency is not inner_dependency

    async def test_wrapped_dependency_fires_triggers(self, inner: SqlResource[Any, Any]) -> None:
        trigger = RecordingTrigger()
        builder = TriggeredDependencyBuilder(
            resource_triggers={inner.get_resource_path(): [trigger]}, background=False
        )
        manifest = Manifest(resources=[inner])
        async with manifest:
            app = create_app(manifest, dependency_builder=builder)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/threads", json={"title": "hi"})
                assert response.status_code == 201
        assert len(trigger.calls) == 1

    async def test_composes_with_an_inner_builder(self, inner: SqlResource[Any, Any]) -> None:
        recorded = _RecordingInnerBuilder()
        trigger = RecordingTrigger()
        builder = TriggeredDependencyBuilder(
            inner=recorded,
            resource_triggers={inner.get_resource_path(): [trigger]},
            background=False,
        )
        manifest = Manifest(resources=[inner])
        async with manifest:
            app = create_app(manifest, dependency_builder=builder)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/threads", json={"title": "hi"})
                assert response.status_code == 201
        # Both the inner builder's own dependency ran *and* the trigger fired —
        # real composition, not a replacement.
        assert recorded.marker == ["reached"]
        assert len(trigger.calls) == 1

    async def test_principal_dependency_delegates_to_inner(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        recorded = _RecordingInnerBuilder()
        builder = TriggeredDependencyBuilder(inner=recorded)
        principal_dep = builder.get_principal_dependency()
        assert principal_dep is not None
        assert await principal_dep() == "recorded"

    def test_does_not_double_wrap_a_triggered_resource(self, inner: SqlResource[Any, Any]) -> None:
        own_trigger = RecordingTrigger(label="own")
        already_wrapped = TriggeredResource(inner, on_edit=[own_trigger], background=False)
        other_trigger = RecordingTrigger(label="config")
        recorded = _RecordingInnerBuilder()
        builder = TriggeredDependencyBuilder(
            inner=recorded,
            resource_triggers={already_wrapped.get_resource_path(): [other_trigger]},
        )
        expected = recorded.get_service_dependency(already_wrapped)
        # A resource that already fires its own on_edit triggers is passed
        # through unchanged -- the constructor list wins, never a second,
        # config-driven wrapper.
        assert builder.get_service_dependency(already_wrapped) is expected

    def test_from_config_builds_the_resource_triggers_map(self) -> None:
        trigger = _WebhookLikeTrigger()
        config = TriggerConfig(triggers=[TriggerEntry(resource_path="threads", trigger=trigger)])
        builder = TriggeredDependencyBuilder.from_config(config)
        assert builder.resource_triggers == {"threads": [trigger]}
        assert isinstance(builder.inner, OpenDependencyBuilder)

    async def test_background_survives_the_request_and_is_drained_as_a_manager(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        """The per-request-exit fix, through the HTTP + builder composition.

        ``get_service_dependency(resource)`` runs once per resource, at
        registration, so the ``TriggerRunner`` it creates there is shared by
        every request's ``TriggeredService`` -- a background run outlives the
        single request that started it. Adding the *same builder instance* to
        ``Manifest(managers=[...])`` is what drains it, on app shutdown.
        """
        trigger = SlowTrigger()
        builder = TriggeredDependencyBuilder(
            resource_triggers={inner.get_resource_path(): [trigger]}, background=True
        )
        manifest = Manifest(resources=[inner], managers=[builder])
        async with manifest:
            app = create_app(manifest, dependency_builder=builder)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/threads", json={"title": "hi"})
                assert response.status_code == 201
            # The request's own (per-request) dependency has already exited --
            # the response came back -- yet the background run is untouched.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert trigger._started is True
            assert trigger._cancelled is False
        # Manifest exit -> managers exit (after resources) -> the builder
        # drains every runner it created.
        assert trigger._cancelled is True
