"""Tests for ``resourcey.triggers.redis_trigger`` (issues #17 / #155).

Exercised against real code paths: a real ``RedisTrigger.callback()`` call, a
real :class:`~resourcey.realtime.realtime_channel.InMemoryChannel` (no
mocking of the publish/subscribe seam), and a real ``SqlResource`` /
``TriggeredResource`` / ``TriggeredService`` over an in-memory SQLite database
for the end-to-end integration tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.service import Create, Delete, Update
from resourcey.realtime.realtime_channel import InMemoryChannel
from resourcey.realtime.realtime_event import EventKind
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.redis_trigger import RedisTrigger
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.view.resource_view import ResourceView


class RedisTriggerBase(DeclarativeBase):
    pass


class Thread(RedisTriggerBase):
    __tablename__ = "redis_trigger_threads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(100))
    secret: Mapped[str] = mapped_column(String(100), default="shh")


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
        await conn.run_sync(RedisTriggerBase.metadata.create_all)
    yield resource
    await engine.dispose()


class _FakeResource:
    """A minimal duck-typed double for ``bind_resource``'s unit tests."""

    def __init__(self, *, id_field: str, resource_path: str, read_response: type[Any]) -> None:
        self._id_field = id_field
        self._resource_path = resource_path
        self._read_response = read_response

    def get_id_field(self) -> str:
        return self._id_field

    def get_resource_path(self) -> str:
        return self._resource_path

    def get_rest_models(self) -> Any:
        class _Models:
            read_response = self._read_response

        return _Models()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults_to_an_in_memory_channel(self) -> None:
        trigger = RedisTrigger(resource_path="threads")
        assert isinstance(trigger.channel, InMemoryChannel)

    def test_accepts_an_explicit_channel(self) -> None:
        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel, resource_path="threads")
        assert trigger.channel is channel


# ---------------------------------------------------------------------------
# callback() against a bound resource (projected items)
# ---------------------------------------------------------------------------


class TestCallbackBound:
    async def test_create_publishes_a_projected_created_event(self) -> None:
        from pydantic import BaseModel

        class ThreadDTO(BaseModel):
            id: uuid.UUID
            title: str
            secret: str

        class ThreadRead(BaseModel):
            id: uuid.UUID
            title: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        trigger.bind_resource(
            _FakeResource(id_field="id", resource_path="threads", read_response=ThreadRead)
        )
        row = ThreadDTO(id=uuid.uuid4(), title="hi", secret="shh")
        sub = channel.subscribe()
        await trigger.callback([Create(item=row)], [row])
        event = await sub.__anext__()
        assert event.resource == "threads"
        assert event.kind == EventKind.CREATED
        assert event.id == str(row.id)
        assert event.item == {"id": str(row.id), "title": "hi"}
        assert "secret" not in event.item

    async def test_update_publishes_a_projected_updated_event(self) -> None:
        from pydantic import BaseModel

        class ThreadDTO(BaseModel):
            id: uuid.UUID
            title: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        trigger.bind_resource(
            _FakeResource(id_field="id", resource_path="threads", read_response=ThreadDTO)
        )
        row = ThreadDTO(id=uuid.uuid4(), title="updated")
        sub = channel.subscribe()
        await trigger.callback([Update(item=row)], [row])
        event = await sub.__anext__()
        assert event.kind == EventKind.UPDATED
        assert event.item == {"id": str(row.id), "title": "updated"}

    async def test_delete_publishes_a_deleted_event_with_no_item(self) -> None:
        from pydantic import BaseModel

        class ThreadDTO(BaseModel):
            id: uuid.UUID
            title: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        trigger.bind_resource(
            _FakeResource(id_field="id", resource_path="threads", read_response=ThreadDTO)
        )
        target = uuid.uuid4()
        sub = channel.subscribe()
        await trigger.callback([Delete(id=target)], [None])
        event = await sub.__anext__()
        assert event.kind == EventKind.DELETED
        assert event.id == str(target)
        assert event.item is None

    async def test_a_batch_edit_miss_publishes_nothing_for_that_pair(self) -> None:
        from pydantic import BaseModel

        class ThreadDTO(BaseModel):
            id: uuid.UUID
            title: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        trigger.bind_resource(
            _FakeResource(id_field="id", resource_path="threads", read_response=ThreadDTO)
        )
        hit = ThreadDTO(id=uuid.uuid4(), title="hit")
        sub = channel.subscribe()
        # An Update for an absent id yields a None result (a batch_edit miss);
        # only the successful pair should publish.
        await trigger.callback(
            [Update(item=hit), Update(item=ThreadDTO(id=uuid.uuid4(), title="miss"))],
            [hit, None],
        )
        event = await sub.__anext__()
        assert event.id == str(hit.id)
        # Nothing else was published for the miss.
        assert channel._subscribers  # the queue is still registered
        queue = next(iter(channel._subscribers))
        assert queue.empty()

    async def test_custom_id_field_is_resolved_from_the_bound_resource(self) -> None:
        from pydantic import BaseModel

        class Item(BaseModel):
            sku: str
            name: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        trigger.bind_resource(
            _FakeResource(id_field="sku", resource_path="items", read_response=Item)
        )
        row = Item(sku="ABC-1", name="Widget")
        sub = channel.subscribe()
        await trigger.callback([Create(item=row)], [row])
        event = await sub.__anext__()
        assert event.id == "ABC-1"
        assert event.resource == "items"


# ---------------------------------------------------------------------------
# callback() without a bound resource (fallback path)
# ---------------------------------------------------------------------------


class TestCallbackUnbound:
    async def test_explicit_resource_path_is_honoured(self) -> None:
        from pydantic import BaseModel

        class ThreadDTO(BaseModel):
            id: uuid.UUID
            title: str

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel, resource_path="threads")
        row = ThreadDTO(id=uuid.uuid4(), title="hi")
        sub = channel.subscribe()
        await trigger.callback([Create(item=row)], [row])
        event = await sub.__anext__()
        assert event.resource == "threads"
        # No bound resource -> the raw DTO's own fields (no projection).
        assert event.item == {"id": str(row.id), "title": "hi"}

    async def test_no_resource_path_and_no_binding_raises(self) -> None:
        import pytest

        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        with pytest.raises(ValueError, match="no resource path"):
            await trigger.callback([Delete(id=uuid.uuid4())], [None])


# ---------------------------------------------------------------------------
# Integration: TriggeredResource auto-binds and auto-derives everything
# ---------------------------------------------------------------------------


class TestTriggeredResourceIntegration:
    async def test_create_update_delete_publish_through_a_real_sql_resource(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        wrapped = TriggeredResource(inner, on_edit=[trigger])
        sub = channel.subscribe()

        dto_type = wrapped.get_dto_type()

        async with wrapped:
            service = await wrapped.get_service()
            async with service:
                created = await service.create(
                    dto_type.model_validate({"title": "t1", "secret": "s1"})
                )
                event = await sub.__anext__()
                assert event.resource == inner.get_resource_path()
                assert event.kind == EventKind.CREATED
                # No ResourceView narrowing here, so the full read model
                # (including "secret") is projected -- see the next test for
                # the hidden-field case.
                assert event.item == {"id": str(created.id), "title": "t1", "secret": "s1"}

                updated = await service.update(
                    dto_type.model_validate({"id": created.id, "title": "t1-updated"})
                )
                event = await sub.__anext__()
                assert event.kind == EventKind.UPDATED
                assert event.item["title"] == "t1-updated"

                await service.delete(created.id)
                event = await sub.__anext__()
                assert event.kind == EventKind.DELETED
                assert event.id == str(created.id)
                assert event.item is None
                assert updated.title == "t1-updated"

    async def test_a_resource_view_hidden_field_never_appears(
        self, inner: SqlResource[Any, Any]
    ) -> None:
        """A ``ResourceView`` that hides ``secret`` keeps it off the realtime channel."""
        view = ResourceView(
            resource=inner,
            exposed_field_overrides={
                "secret": {
                    "in_read_response": False,
                    "in_search_response": False,
                }
            },
        )
        channel = InMemoryChannel()
        trigger = RedisTrigger(channel=channel)
        wrapped = TriggeredResource(view, on_edit=[trigger])
        sub = channel.subscribe()
        dto_type = wrapped.get_dto_type()

        async with wrapped:
            service = await wrapped.get_service()
            async with service:
                created = await service.create(
                    dto_type.model_validate({"title": "hidden-test", "secret": "top-secret"})
                )
                event = await sub.__anext__()
                assert "secret" not in event.item
                assert event.item == {"id": str(created.id), "title": "hidden-test"}
