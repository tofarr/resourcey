"""Behaviour tests for the ``v2/core`` Resource / Service / Manifest (issue #75).

These exercise the non-HTTP contract: the service as async context manager, the
``ctx`` storage-ownership rule (both strategies), the action declaration and
the startup assertion, and the Manifest lifecycle. The end-to-end HTTP test
lives in ``test_v2_app.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.v2.core.dto import DTO
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import (
    STORAGE_KEY,
    Action,
    NotFoundError,
    Service,
    ServiceError,
    assert_real_actions,
)
from resourcey.v2.sql.resource import SqlResource


class Thread(DTO):
    id: int
    title: str


class Message(DTO):
    id: int
    thread_id: int
    body: str


class RecordingResource(SqlResource[Any]):
    """A SQL resource recording its ``__aexit__`` calls, for the Manifest test."""

    def __init__(self, dto: type[DTO], *, session_factory: Any, order: list[str]) -> None:
        super().__init__(dto, session_factory=session_factory)
        self._order = order

    async def __aexit__(self, *exc: object) -> None:
        self._order.append(self.get_resource_path())
        await super().__aexit__(*exc)


@pytest_asyncio.fixture
async def resources() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], SqlResource[Any], SqlResource[Any]]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)
        await conn.run_sync(messages.metadata.create_all)
    yield maker, threads, messages
    await engine.dispose()


def _dummy_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return async_sessionmaker(engine)


# ---------------------------------------------------------------------------
# Service as async context manager
# ---------------------------------------------------------------------------


async def test_service_methods_raise_before_enter(resources):
    _maker, threads, _messages = resources
    service = threads.get_service()
    with pytest.raises(ServiceError, match="before entering"):
        await service.read(1)


async def test_reentering_an_entered_service_raises(resources):
    _maker, threads, _messages = resources
    service = threads.get_service()
    async with service:
        with pytest.raises(ServiceError, match="already entered"):
            await service.__aenter__()


async def test_entered_property_tracks_context_manager(resources):
    _maker, threads, _messages = resources
    service = threads.get_service()
    assert service.entered is False
    async with service:
        assert service.entered is True
    assert service.entered is False


def test_build_service_on_base_resource_raises():
    class Bare(Resource[Any]):
        pass

    with pytest.raises(ServiceError, match="no storage backend"):
        Bare(Thread).get_service()


# ---------------------------------------------------------------------------
# CRUD + batch + count through the SQL service
# ---------------------------------------------------------------------------


async def test_crud_and_count_and_search(resources):
    _maker, threads, _messages = resources
    ctx: dict[Any, Any] = {}
    async with threads.get_service(ctx) as service:
        created = await service.create(Thread.get_dto_type()(title="hello"))
        assert created.id is not None
        assert created.title == "hello"

        fetched = await service.read(created.id)
        assert fetched.title == "hello"

        updated = await service.update(created.id, Thread.get_dto_type()(title="bye"))
        assert updated.title == "bye"

        assert await service.count() == 1
        page = await service.search(limit=10)
        assert [item.title for item in page.items] == ["bye"]

        await service.delete(created.id)
        assert await service.count() == 0


async def test_read_absent_raises(resources):
    _maker, threads, _messages = resources
    async with threads.get_service() as service:
        with pytest.raises(NotFoundError):
            await service.read(999)


async def test_update_and_delete_absent_raise(resources):
    _maker, threads, _messages = resources
    async with threads.get_service() as service:
        with pytest.raises(NotFoundError):
            await service.update(999, Thread.get_dto_type()(title="x"))
        with pytest.raises(NotFoundError):
            await service.delete(999)


async def test_batch_read_and_batch_edit(resources):
    _maker, threads, _messages = resources
    async with threads.get_service() as service:
        a = await service.create(Thread.get_dto_type()(title="a"))
        b = await service.create(Thread.get_dto_type()(title="b"))
        assert [x.title for x in await service.batch_read([a.id, b.id])] == ["a", "b"]
        # an absent id yields a positional None
        assert (await service.batch_read([a.id, 999]))[1] is None

        edited = await service.batch_edit(
            [(a.id, Thread.get_dto_type()(title="a2")), (999, Thread.get_dto_type()(title="x"))]
        )
        assert edited[0] is not None and edited[0].title == "a2"
        assert edited[1] is None


async def test_search_rejects_sort_and_desc_for_now(resources):
    _maker, threads, _messages = resources
    async with threads.get_service() as service:
        with pytest.raises(NotImplementedError):
            await service.search(sort="title")
        with pytest.raises(NotImplementedError):
            await service.search(desc=True)
        with pytest.raises(NotImplementedError):
            await service.search(filters={"title": "x"})


async def test_search_orders_by_id_ascending(resources):
    _maker, threads, _messages = resources
    async with threads.get_service() as service:
        await service.create(Thread.get_dto_type()(title="a"))
        await service.create(Thread.get_dto_type()(title="b"))
        page = await service.search(limit=10)
        assert [item.title for item in page.items] == ["a", "b"]


# ---------------------------------------------------------------------------
# Storage ownership
# ---------------------------------------------------------------------------


async def test_owner_opens_and_commits_its_own_storage(resources):
    _maker, threads, _messages = resources
    ctx: dict[Any, Any] = {}
    service = threads.get_service(ctx)
    assert STORAGE_KEY not in ctx
    async with service:
        assert STORAGE_KEY in ctx
        await service.create(Thread.get_dto_type()(title="owned"))
    # The opener closed and cleared its storage on exit.
    assert STORAGE_KEY not in ctx
    # And the row was committed.
    async with threads.get_service() as fresh:
        assert await fresh.count() == 1


async def test_reusing_seeded_storage_does_not_commit_or_close(resources):
    maker, threads, _messages = resources
    shared = maker()
    ctx: dict[Any, Any] = {STORAGE_KEY: shared}
    service = threads.get_service(ctx)
    async with service:
        await service.create(Thread.get_dto_type()(title="shared"))
        assert ctx[STORAGE_KEY] is shared
    # The reusing service neither closed the session nor dropped the key.
    assert STORAGE_KEY in ctx
    assert shared.is_active  # still usable: the owner left it open
    await shared.rollback()
    await shared.close()


async def test_session_per_service_shares_one_storage_across_resources(resources):
    _maker, threads, messages = resources
    ctx: dict[Any, Any] = {}
    async with threads.get_service(ctx) as thread_service:
        thread = await thread_service.create(Thread.get_dto_type()(title="t"))
        async with messages.get_service(ctx) as message_service:
            # The second service adopted the first's session.
            assert message_service._session is thread_service._session
            await message_service.create(Message.get_dto_type()(thread_id=thread.id, body="hello"))
    async with messages.get_service() as fresh:
        assert await fresh.count() == 1


async def test_exception_in_owned_storage_rolls_back(resources):
    _maker, threads, _messages = resources
    with pytest.raises(RuntimeError):
        async with threads.get_service() as service:
            await service.create(Thread.get_dto_type()(title="rolled-back"))
            raise RuntimeError("boom")
    async with threads.get_service() as fresh:
        assert await fresh.count() == 0


# ---------------------------------------------------------------------------
# Actions / exposure
# ---------------------------------------------------------------------------


async def test_default_supported_actions_is_every_action(resources):
    _maker, threads, _messages = resources
    assert threads.get_supported_actions() == frozenset(Action)


async def test_exposed_resource_defaults_to_self(resources):
    _maker, threads, _messages = resources
    assert threads.get_exposed_resource() is threads


def test_assert_real_actions_accepts_real_actions():
    assert_real_actions("X", frozenset({Action.READ, Action.CREATE}))


def test_assert_real_actions_rejects_non_action_members():
    with pytest.raises(ServiceError, match="must be an Action"):
        assert_real_actions("X", frozenset({"creat"}))


def test_manifest_rejects_a_typo_in_supported_actions():
    class Bad(SqlResource[Any]):
        def get_supported_actions(self) -> frozenset[Any]:
            return frozenset({"creat"})

    with pytest.raises(ServiceError):
        Manifest(resources=[Bad(Thread, session_factory=_dummy_factory())])


def test_manifest_accepts_a_narrowed_real_action_set():
    class Narrow(SqlResource[Any]):
        def get_supported_actions(self) -> frozenset[Any]:
            return frozenset({Action.READ})

    manifest = Manifest(resources=[Narrow(Thread, session_factory=_dummy_factory())])
    assert manifest.resource_names() == ("threads",)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


async def test_manifest_lifecycle_and_lookup(resources):
    _maker, threads, messages = resources
    manifest: Manifest[Any] = Manifest(resources=[threads, messages])
    assert manifest.resource_names() == ("threads", "messages")
    assert manifest.get_resource("messages") is messages
    with pytest.raises(KeyError):
        manifest.get_resource("nope")
    async with manifest:
        assert manifest.entered is True
    assert manifest.entered is False


async def test_manifest_double_entry_raises(resources):
    _maker, threads, messages = resources
    manifest: Manifest[Any] = Manifest(resources=[threads, messages])
    async with manifest:
        with pytest.raises(ServiceError, match="already entered"):
            await manifest.__aenter__()


async def test_manifest_exits_resources_in_reverse(resources):
    maker, _threads, _messages = resources
    order: list[str] = []
    threads = RecordingResource(Thread, session_factory=maker, order=order)
    messages = RecordingResource(Message, session_factory=maker, order=order)
    async with Manifest(resources=[threads, messages]):
        pass
    assert order == ["messages", "threads"]


async def test_service_dependency_yields_entered_service(resources):
    _maker, threads, _messages = resources

    class FakeRequest:
        class state:  # noqa: N801
            pass

    agen = threads.get_service_dependency(FakeRequest())  # type: ignore[arg-type]
    service = await agen.__anext__()
    assert service.entered is True
    await agen.aclose()


async def test_get_service_without_ctx_creates_a_private_context(resources):
    _maker, threads, _messages = resources
    service = threads.get_service()
    async with service as entered:
        await entered.create(Thread.get_dto_type()(title="private"))
    async with threads.get_service() as fresh:
        assert await fresh.count() == 1


async def test_resource_double_entry_raises(resources):
    _maker, threads, _messages = resources
    async with threads:
        with pytest.raises(ServiceError, match="already entered"):
            await threads.__aenter__()


def test_get_dto_and_rest_models(resources):
    _maker, threads, _messages = resources
    assert threads.get_id_field() == "id"
    assert threads.get_search_filter_type() is None
    assert threads.get_cache_strategy() is None
    assert issubclass(threads.get_dto_type(), BaseModel)
    assert set(vars(threads.get_rest_models())) == {
        "create_request",
        "create_response",
        "update_request",
        "update_response",
        "read_response",
        "search_response",
    }


def test_resource_path_override():
    resource = SqlResource(Thread, session_factory=_dummy_factory(), path="/custom/threads")
    assert resource.get_resource_path() == "custom/threads"


class ByCode(DTO, id_field_name="code"):
    code: str
    name: str


@pytest_asyncio.fixture
async def code_resource() -> AsyncIterator[tuple[SqlResource[Any], Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(ByCode, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(resource.metadata.create_all)
    yield resource, maker
    await engine.dispose()


def test_get_id_field_reads_the_dto_declaration(code_resource):
    resource, _maker = code_resource
    assert resource.get_id_field() == "code"
    assert [c.name for c in resource.table.primary_key.columns] == ["code"]


def test_non_integer_identifier_is_a_plain_primary_key(code_resource):
    resource, _maker = code_resource
    column = resource.table.c["code"]
    assert column.primary_key is True
    # A string key is not an autoincrement integer; the caller must supply it.
    assert column.autoincrement is not True
    assert resource.table.c["name"].nullable is True


async def test_crud_with_a_custom_identifier(code_resource):
    resource, _maker = code_resource
    async with resource.get_service() as service:
        created = await service.create(ByCode.get_dto_type()(code="US", name="United States"))
        assert (created.code, created.name) == ("US", "United States")

        read = await service.read("US")
        assert read.name == "United States"

        updated = await service.update("US", ByCode.get_dto_type()(name="USA"))
        assert updated.name == "USA"
        # The identifier is never overwritten by an update payload.
        assert updated.code == "US"

        await service.delete("US")
        with pytest.raises(NotFoundError):
            await service.read("US")


def test_table_and_metadata_properties():
    resource = SqlResource(Thread, session_factory=_dummy_factory())
    assert resource.table.name == "threads"
    assert resource.metadata is not None


def test_full_action_enum_members():
    assert {a.value for a in Action} == {
        "create",
        "read",
        "update",
        "delete",
        "search",
        "count",
        "batch_read",
        "batch_edit",
    }


def test_ctx_is_a_plain_mutable_mapping(resources):
    _maker, threads, _messages = resources
    ctx: MutableMapping[Any, Any] = {}

    async def run() -> None:
        async with threads.get_service(ctx) as service:
            await service.count()
            assert isinstance(ctx, dict)

    asyncio.run(run())


async def test_base_service_actions_are_raising_safety_nets():
    base: Service[Any] = Service()
    async with base:
        with pytest.raises(NotImplementedError):
            await base.create(None)
        with pytest.raises(NotImplementedError):
            await base.read(1)
        with pytest.raises(NotImplementedError):
            await base.update(1, None)
        with pytest.raises(NotImplementedError):
            await base.delete(1)
        with pytest.raises(NotImplementedError):
            await base.search()
        with pytest.raises(NotImplementedError):
            await base.count()
        with pytest.raises(NotImplementedError):
            await base.batch_read([])
        with pytest.raises(NotImplementedError):
            await base.batch_edit([])
