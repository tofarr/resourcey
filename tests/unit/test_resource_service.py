"""Tests for ``ResourceService`` and ``register`` (issue #2).

Covers:
- All seven actions (create, read, update, delete, search, batch_read,
  batch_edit) at the service level (direct async calls with an AsyncSession)
  and at the HTTP level (httpx against a FastAPI app with the registered
  routes).
- Error mapping: NotFoundError -> 404, InvalidInputError -> 400 (bad sort,
  filter params when no filter declared, unknown filter field),
  IntegrityError -> 409 (unique constraint).
- Conventions: kebab-case URL paths (``batch-read``, ``batch-edit``),
  ``batch_read`` as a GET with repeated ``id`` query params.
- Escape hatches: ``register`` skips a route already present; a custom
  ``session_dependency`` / ``repository_cls`` / ``authorize`` override works.
- Search filter integration with a declared ``SearchFilter``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field
from sqlalchemy import Column, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.resource.base import BaseResource, ResourceyBase
from resourcey.resource.errors import InvalidInputError, NotFoundError
from resourcey.resource.field import ResourceyField
from resourcey.resource.repository import ResourceRepository
from resourcey.resource.service import (
    Page,
    ResourceService,
    ResourceServiceError,
    register_error_handlers,
)
from resourcey.util.search_filter import BaseSearchFilter

# ---------------------------------------------------------------------------
# Resource fixtures
# ---------------------------------------------------------------------------


class SvcWidget(BaseResource):
    """A simple resource for CRUD tests — int id, required label, optional size."""

    id: int
    label: str
    size: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SvcGadget(BaseResource):
    """A resource with a unique constraint to exercise the 409 conflict path."""

    id: int
    serial: Annotated[str, ResourceyField(column=Column("serial", String(64), unique=True))]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_GadgetOrm = SvcGadget.get_sql_alchemy_model()


class GadgetSearchFilter(BaseSearchFilter[_GadgetOrm]):
    """Declarative filter for SvcGadget — opted in via get_search_filter_type."""

    serial__eq: str | None = None
    serial__contains: str | None = None


class SvcFilterableWidget(BaseResource):
    """A resource that opts into filtering via a declared search filter class."""

    id: int
    label: str
    size: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[BaseSearchFilter] | None:
        model = cls.get_sql_alchemy_model()

        class _Filter(BaseSearchFilter[model]):  # type: ignore[valid-type]
            label__eq: str | None = None
            label__contains: str | None = None
            size__eq: int | None = None
            size__gte: int | None = None
            size__in: list[int] | None = None

        return _Filter


class SvcUnsortableWidget(BaseResource):
    """A resource whose every field is opted out of sorting.

    Exercises the ``sort`` / ``desc`` params being omitted from the OpenAPI
    schema and ``?sort=`` being rejected at runtime (#30).
    """

    id: Annotated[int, ResourceyField(sortable=False)]
    label: Annotated[str, ResourceyField(sortable=False)]
    created_at: Annotated[
        datetime, Field(default_factory=lambda: datetime.now(UTC)), ResourceyField(sortable=False)
    ]


class SvcDtWidget(BaseResource):
    """A resource with a creatable, sortable datetime field for cursor type round-trip tests."""

    id: int
    ts: datetime


# Resolve ORM models eagerly so metadata is populated before table creation.
for _r in (SvcWidget, SvcGadget, SvcFilterableWidget, SvcUnsortableWidget, SvcDtWidget):
    _r.get_sql_alchemy_model()


# ---------------------------------------------------------------------------
# Session + app fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """A shared in-memory SQLite engine + session factory (StaticPool so all
    sessions in a test see the same database — required for cross-request
    persistence in the HTTP tests)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """A single session for service-level tests (no auto-commit; caller controls)."""
    async with session_factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def client_factory(session_factory: async_sessionmaker[AsyncSession]):
    """Factory building an httpx AsyncClient against a FastAPI app with the
    given service's routes registered + error handlers installed."""

    def _build(service: ResourceService) -> AsyncClient:
        app = FastAPI()
        service.register(app)
        register_error_handlers(app)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return _build


# ---------------------------------------------------------------------------
# Service-level tests (direct async calls)
# ---------------------------------------------------------------------------


class TestServiceCreate:
    @pytest.mark.asyncio
    async def test_create_returns_read_model_with_generated_id(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        result = await svc.create(session, SvcWidget.get_create_model()(label="gadget"))
        assert result.id == 1
        assert result.label == "gadget"
        assert result.size == 0
        assert result.created_at is not None

    @pytest.mark.asyncio
    async def test_create_populates_default_factory_timestamps(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        result = await svc.create(session, SvcWidget.get_create_model()(label="x"))
        # created_at is not creatable (excluded from create model) but the
        # repository supplements its default_factory so it is never NULL.
        assert result.created_at is not None

    @pytest.mark.asyncio
    async def test_create_drops_missing_optional_fields(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        # size is optional (default 0); omitting it should store 0, not MISSING.
        result = await svc.create(session, SvcWidget.get_create_model()(label="x"))
        assert result.size == 0


class TestServiceRead:
    @pytest.mark.asyncio
    async def test_read_returns_entity(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        created = await svc.create(session, SvcWidget.get_create_model()(label="g"))
        result = await svc.read(session, created.id)
        assert result.label == "g"

    @pytest.mark.asyncio
    async def test_read_missing_raises_not_found(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        with pytest.raises(NotFoundError):
            await svc.read(session, 999)


class TestServiceUpdate:
    @pytest.mark.asyncio
    async def test_update_applies_patch(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        created = await svc.create(session, SvcWidget.get_create_model()(label="g", size=1))
        result = await svc.update(session, created.id, SvcWidget.get_update_model()(size=99))
        assert result.size == 99
        assert result.label == "g"  # untouched (PATCH semantics)

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        with pytest.raises(NotFoundError):
            await svc.update(session, 999, SvcWidget.get_update_model()(size=1))

    @pytest.mark.asyncio
    async def test_update_empty_payload_returns_current(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        created = await svc.create(session, SvcWidget.get_create_model()(label="g"))
        # An update model with no fields set — nothing to change.
        result = await svc.update(session, created.id, SvcWidget.get_update_model()())
        assert result.id == created.id


class TestServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_entity(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        created = await svc.create(session, SvcWidget.get_create_model()(label="g"))
        await svc.delete(session, created.id)
        with pytest.raises(NotFoundError):
            await svc.read(session, created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        with pytest.raises(NotFoundError):
            await svc.delete(session, 999)


class TestServiceSearch:
    @pytest.mark.asyncio
    async def test_search_returns_page_with_metadata(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in range(3):
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, limit=10)
        assert isinstance(page, Page)
        assert len(page.items) == 3
        assert page.limit == 10
        # All 3 rows fit in one page -> no next cursor.
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_cursor_pagination(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in range(5):
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, limit=2)
        assert len(page.items) == 2
        assert page.next_cursor is not None
        page2 = await svc.search(session, limit=2, cursor=page.next_cursor)
        assert len(page2.items) == 2
        # Cursor advances — no overlap with the first page.
        assert {item.id for item in page2.items}.isdisjoint({item.id for item in page.items})
        page3 = await svc.search(session, limit=2, cursor=page2.next_cursor)
        assert len(page3.items) == 1
        assert page3.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_cursor_pagination_with_sort(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in [3, 1, 2]:
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, limit=2, sort="size")
        assert [item.size for item in page.items] == [1, 2]
        assert page.next_cursor is not None
        page2 = await svc.search(session, limit=2, sort="size", cursor=page.next_cursor)
        assert [item.size for item in page2.items] == [3]
        assert page2.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_invalid_cursor_raises(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        await svc.create(session, SvcWidget.get_create_model()(label="g0", size=0))
        with pytest.raises(InvalidInputError):
            await svc.search(session, cursor="not-a-valid-cursor")

    @pytest.mark.asyncio
    async def test_search_cursor_sort_mismatch_raises(self, session: AsyncSession) -> None:
        """A cursor built for sort=size must not be reused under a different sort."""
        svc = ResourceService(SvcWidget)
        for i in range(5):
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, limit=2, sort="size")
        assert page.next_cursor is not None
        # Reusing the size-sorted cursor under no sort -> 400.
        with pytest.raises(InvalidInputError):
            await svc.search(session, limit=2, cursor=page.next_cursor)
        # Reusing under a different sort field -> 400.
        with pytest.raises(InvalidInputError):
            await svc.search(session, limit=2, sort="label", cursor=page.next_cursor)
        # Reusing under a different direction -> 400.
        with pytest.raises(InvalidInputError):
            await svc.search(session, limit=2, sort="size", desc=True, cursor=page.next_cursor)

    @pytest.mark.asyncio
    async def test_search_cursor_datetime_sort_round_trip(self, session: AsyncSession) -> None:
        """Cursor round-trip preserves a native datetime sort key (not stringified).

        SQLite stores datetimes without tzinfo, so we compare naive values;
        the point is that the cursor binds a native ``datetime`` (not a
        string) to the keyset predicate and the round-trip yields the right
        page ordering.
        """
        svc = ResourceService(SvcDtWidget)
        for i in range(5):
            await svc.create(
                session,
                SvcDtWidget.get_create_model()(ts=datetime(2026, 1, i + 1, 12, 0, 0, tzinfo=UTC)),
            )
        page = await svc.search(session, limit=2, sort="ts")
        assert [item.ts.replace(tzinfo=None) for item in page.items] == [
            datetime(2026, 1, 1, 12, 0, 0),
            datetime(2026, 1, 2, 12, 0, 0),
        ]
        assert page.next_cursor is not None
        page2 = await svc.search(session, limit=2, sort="ts", cursor=page.next_cursor)
        assert [item.ts.replace(tzinfo=None) for item in page2.items] == [
            datetime(2026, 1, 3, 12, 0, 0),
            datetime(2026, 1, 4, 12, 0, 0),
        ]
        page3 = await svc.search(session, limit=2, sort="ts", cursor=page2.next_cursor)
        assert len(page3.items) == 1
        assert page3.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_sort_ascending(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in [3, 1, 2]:
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, sort="size")
        assert [item.size for item in page.items] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_search_sort_descending(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in [3, 1, 2]:
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        page = await svc.search(session, sort="size", desc=True)
        assert [item.size for item in page.items] == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_search_sort_unknown_field_raises(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        with pytest.raises(InvalidInputError):
            await svc.search(session, sort="nonsense")

    @pytest.mark.asyncio
    async def test_search_limit_below_one_raises(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        with pytest.raises(InvalidInputError):
            await svc.search(session, limit=0)

    @pytest.mark.asyncio
    async def test_search_limit_capped_to_max(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        page = await svc.search(session, limit=999)
        assert page.limit == 100  # _MAX_LIMIT

    @pytest.mark.asyncio
    async def test_search_with_declared_filter(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcFilterableWidget)
        filter_cls = SvcFilterableWidget.get_search_filter_type()
        assert filter_cls is not None
        for i in range(3):
            await svc.create(session, SvcFilterableWidget.get_create_model()(label=f"g{i}", size=i))
        filters = filter_cls(size__gte=2)
        page = await svc.search(session, filters=filters)
        assert len(page.items) == 1
        assert page.items[0].size == 2


class TestServiceCount:
    @pytest.mark.asyncio
    async def test_count_all(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        for i in range(3):
            await svc.create(session, SvcWidget.get_create_model()(label=f"g{i}", size=i))
        assert await svc.count(session) == 3

    @pytest.mark.asyncio
    async def test_count_with_filter(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcFilterableWidget)
        filter_cls = SvcFilterableWidget.get_search_filter_type()
        assert filter_cls is not None
        for i in range(5):
            await svc.create(session, SvcFilterableWidget.get_create_model()(label=f"g{i}", size=i))
        assert await svc.count(session, filters=filter_cls(size__gte=3)) == 2

    @pytest.mark.asyncio
    async def test_count_empty(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        assert await svc.count(session) == 0


class TestServiceBatchRead:
    @pytest.mark.asyncio
    async def test_batch_read_returns_in_input_order(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        a = await svc.create(session, SvcWidget.get_create_model()(label="a"))
        b = await svc.create(session, SvcWidget.get_create_model()(label="b"))
        result = await svc.batch_read(session, [b.id, a.id])
        assert [r.id for r in result] == [b.id, a.id]

    @pytest.mark.asyncio
    async def test_batch_read_omits_absent_ids(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        a = await svc.create(session, SvcWidget.get_create_model()(label="a"))
        result = await svc.batch_read(session, [a.id, 999])
        assert len(result) == 1
        assert result[0].id == a.id

    @pytest.mark.asyncio
    async def test_batch_read_empty_list(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        result = await svc.batch_read(session, [])
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_read_deduplicates_ids(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        a = await svc.create(session, SvcWidget.get_create_model()(label="a"))
        result = await svc.batch_read(session, [a.id, a.id])
        assert len(result) == 1


class TestServiceBatchEdit:
    @pytest.mark.asyncio
    async def test_batch_edit_applies_updates(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        a = await svc.create(session, SvcWidget.get_create_model()(label="a", size=1))
        b = await svc.create(session, SvcWidget.get_create_model()(label="b", size=2))
        results = await svc.batch_edit(
            session,
            [
                (a.id, SvcWidget.get_update_model()(size=10)),
                (b.id, SvcWidget.get_update_model()(size=20)),
            ],
        )
        assert {r.id: r.size for r in results} == {a.id: 10, b.id: 10 + 10}

    @pytest.mark.asyncio
    async def test_batch_edit_skips_absent_ids(self, session: AsyncSession) -> None:
        svc = ResourceService(SvcWidget)
        a = await svc.create(session, SvcWidget.get_create_model()(label="a"))
        results = await svc.batch_edit(
            session,
            [
                (a.id, SvcWidget.get_update_model()(size=5)),
                (999, SvcWidget.get_update_model()(size=9)),  # absent -> skipped
            ],
        )
        assert len(results) == 1
        assert results[0].id == a.id


class TestServiceAuthorize:
    @pytest.mark.asyncio
    async def test_authorize_called_before_action(self, session: AsyncSession) -> None:
        calls: list[str] = []

        class GuardedService(ResourceService):
            async def authorize(self, s, action, **ctx) -> None:
                calls.append(action)

        svc = GuardedService(SvcWidget)
        await svc.create(session, SvcWidget.get_create_model()(label="x"))
        assert "create" in calls


class TestServiceRepositoryOverride:
    @pytest.mark.asyncio
    async def test_custom_repository_cls_used(self, session: AsyncSession) -> None:
        class CountingRepo(ResourceRepository):
            insert_count = 0

            async def insert(self, sess, payload, *, context=None):
                CountingRepo.insert_count += 1
                return await super().insert(sess, payload, context=context)

        svc = ResourceService(SvcWidget, repository_cls=CountingRepo)
        await svc.create(session, SvcWidget.get_create_model()(label="x"))
        assert CountingRepo.insert_count == 1


# ---------------------------------------------------------------------------
# HTTP-level tests (register + httpx)
# ---------------------------------------------------------------------------


class TestRegisterRoutes:
    @pytest.mark.asyncio
    async def test_all_seven_routes_registered(self, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        app = FastAPI()
        router = svc.register(app)
        paths = {(r.path, next(iter(r.methods))) for r in router.routes}
        assert ("/svc-widgets", "POST") in paths
        assert ("/svc-widgets/{id}", "GET") in paths
        assert ("/svc-widgets/{id}", "PATCH") in paths
        assert ("/svc-widgets/{id}", "DELETE") in paths
        assert ("/svc-widgets", "GET") in paths
        assert ("/svc-widgets/batch-read", "GET") in paths
        assert ("/svc-widgets/batch-edit", "POST") in paths

    @pytest.mark.asyncio
    async def test_register_returns_router(self, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        app = FastAPI()
        router = svc.register(app)
        assert len(router.routes) == 8

    @pytest.mark.asyncio
    async def test_register_with_prefix(self, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        app = FastAPI()
        svc.register(app, prefix="/api/v1")
        register_error_handlers(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/svc-widgets", json={"label": "g"})
            assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_register_without_session_raises(self) -> None:
        svc = ResourceService(SvcWidget)
        app = FastAPI()
        with pytest.raises(ResourceServiceError):
            svc.register(app)

    @pytest.mark.asyncio
    async def test_custom_session_dependency(self, session_factory) -> None:
        svc = ResourceService(SvcWidget)

        async def dep():
            async with session_factory() as sess:
                yield sess
                await sess.commit()

        app = FastAPI()
        svc.register(app, session_dependency=dep)
        register_error_handlers(app)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/svc-widgets", json={"label": "g"})
            assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_escape_hatch_custom_route_preserved(self, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        app = FastAPI()
        # Register a custom GET /svc-widgets route BEFORE the service — it should
        # be kept (not overwritten) by register's escape hatch.
        from fastapi import APIRouter

        custom = APIRouter()

        @custom.get("/svc-widgets")
        async def custom_search():
            return {"custom": True}

        app.include_router(custom)
        svc.register(app)
        register_error_handlers(app)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/svc-widgets")
            assert r.json() == {"custom": True}


class TestHttpCrud:
    @pytest.mark.asyncio
    async def test_create_returns_201(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.post("/svc-widgets", json={"label": "gadget", "size": 5})
            assert r.status_code == 201
            assert r.json()["label"] == "gadget"
            assert r.json()["id"] == 1

    @pytest.mark.asyncio
    async def test_read_returns_200(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.post("/svc-widgets", json={"label": "g"})
            wid = r.json()["id"]
            r = await client.get(f"/svc-widgets/{wid}")
            assert r.status_code == 200
            assert r.json()["label"] == "g"

    @pytest.mark.asyncio
    async def test_read_missing_returns_404_envelope(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets/999")
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_update_returns_200(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            wid = (await client.post("/svc-widgets", json={"label": "g", "size": 1})).json()["id"]
            r = await client.patch(f"/svc-widgets/{wid}", json={"size": 99})
            assert r.status_code == 200
            assert r.json()["size"] == 99

    @pytest.mark.asyncio
    async def test_delete_returns_204(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            wid = (await client.post("/svc-widgets", json={"label": "g"})).json()["id"]
            r = await client.delete(f"/svc-widgets/{wid}")
            assert r.status_code == 204
            r2 = await client.get(f"/svc-widgets/{wid}")
            assert r2.status_code == 404

    @pytest.mark.asyncio
    async def test_search_returns_page(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            for i in range(3):
                await client.post("/svc-widgets", json={"label": f"g{i}", "size": i})
            r = await client.get("/svc-widgets?limit=2")
            assert r.status_code == 200
            body = r.json()
            assert "total" not in body
            assert "offset" not in body
            assert len(body["items"]) == 2
            assert body["next_cursor"] is not None
            # Follow the cursor to the remaining item.
            r2 = await client.get(f"/svc-widgets?limit=2&cursor={body['next_cursor']}")
            body2 = r2.json()
            assert len(body2["items"]) == 1
            assert body2["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_search_rejects_offset_param(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets?offset=0")
            # offset is no longer a recognized param; FastAPI ignores unknown
            # query params, so the request succeeds but the response has no
            # "offset" field.
            assert r.status_code == 200
            assert "offset" not in r.json()

    @pytest.mark.asyncio
    async def test_count_endpoint(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            for i in range(3):
                await client.post("/svc-widgets", json={"label": f"g{i}", "size": i})
            r = await client.get("/svc-widgets/count")
            assert r.status_code == 200
            assert r.json() == 3

    @pytest.mark.asyncio
    async def test_count_with_filter(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcFilterableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            for i in range(5):
                await client.post("/svc-filterable-widgets", json={"label": f"g{i}", "size": i})
            r = await client.get("/svc-filterable-widgets/count?size__gte=3")
            assert r.status_code == 200
            assert r.json() == 2

    @pytest.mark.asyncio
    async def test_count_rejects_sort_and_limit(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets/count?sort=size")
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_input"
            r = await client.get("/svc-widgets/count?limit=10")
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_search_sort_via_query(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            for s in [3, 1, 2]:
                await client.post("/svc-widgets", json={"label": "g", "size": s})
            r = await client.get("/svc-widgets?sort=size&desc=true")
            assert [i["size"] for i in r.json()["items"]] == [3, 2, 1]


class TestHttpBatchRead:
    @pytest.mark.asyncio
    async def test_batch_read_repeated_id_params(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            a = (await client.post("/svc-widgets", json={"label": "a"})).json()["id"]
            b = (await client.post("/svc-widgets", json={"label": "b"})).json()["id"]
            r = await client.get(f"/svc-widgets/batch-read?id={a}&id={b}&id=999")
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 2
            assert [item["id"] for item in body] == [a, b]

    @pytest.mark.asyncio
    async def test_batch_read_no_ids_returns_empty(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets/batch-read")
            assert r.status_code == 200
            assert r.json() == []


class TestHttpBatchEdit:
    @pytest.mark.asyncio
    async def test_batch_edit_applies(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            a = (await client.post("/svc-widgets", json={"label": "a", "size": 1})).json()["id"]
            b = (await client.post("/svc-widgets", json={"label": "b", "size": 2})).json()["id"]
            r = await client.post(
                "/svc-widgets/batch-edit",
                json=[{"id": a, "size": 10}, {"id": b, "label": "renamed"}],
            )
            assert r.status_code == 200
            body = r.json()
            assert {item["id"]: item["size"] for item in body} == {a: 10, b: 2}
            assert next(i for i in body if i["id"] == b)["label"] == "renamed"


class TestHttpErrors:
    @pytest.mark.asyncio
    async def test_bad_sort_returns_422(self, client_factory, session_factory) -> None:
        # ``sort`` is an enum of sortable fields; an unknown value is rejected
        # by FastAPI's request validation (422), consistent with how typed
        # filter params behave (#31).
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets?sort=nonsense")
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_sort_injection_attempt_returns_422(
        self, client_factory, session_factory
    ) -> None:
        # The enum validates the value, so a SQL-injection-style payload never
        # reaches the query layer.
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets?sort=id;%20DROP%20TABLE%20users")
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_sort_on_sortless_resource_returns_400(
        self, client_factory, session_factory
    ) -> None:
        # A resource with no sortable fields exposes no ``sort`` param; a
        # residual check rejects ``?sort=`` with 400 invalid_input.
        svc = ResourceService(SvcUnsortableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-unsortable-widgets?sort=id")
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_filter_when_none_declared_returns_400(
        self, client_factory, session_factory
    ) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-widgets?bogus__eq=x")
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_unknown_filter_field_returns_400(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcFilterableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            r = await client.get("/svc-filterable-widgets?nonsense__eq=x")
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_unique_constraint_returns_409(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcGadget, session_factory=session_factory)
        async with client_factory(svc) as client:
            await client.post("/svc-gadgets", json={"serial": "SN1"})
            r = await client.post("/svc-gadgets", json={"serial": "SN1"})
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "conflict"

    @pytest.mark.asyncio
    async def test_validation_error_returns_422(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            # label is required; omitting it should fail validation.
            r = await client.post("/svc-widgets", json={"size": 1})
            assert r.status_code == 422


class TestHttpFiltering:
    @pytest.mark.asyncio
    async def test_declared_filter_applies(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcFilterableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            await client.post("/svc-filterable-widgets", json={"label": "apple", "size": 1})
            await client.post("/svc-filterable-widgets", json={"label": "banana", "size": 2})
            r = await client.get("/svc-filterable-widgets?label__contains=app")
            assert r.status_code == 200
            body = r.json()
            assert len(body["items"]) == 1
            assert body["items"][0]["label"] == "apple"

    @pytest.mark.asyncio
    async def test_declared_filter_eq(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcFilterableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            await client.post("/svc-filterable-widgets", json={"label": "a", "size": 1})
            await client.post("/svc-filterable-widgets", json={"label": "b", "size": 2})
            r = await client.get("/svc-filterable-widgets?size__eq=2")
            assert len(r.json()["items"]) == 1
            assert r.json()["items"][0]["label"] == "b"

    @pytest.mark.asyncio
    async def test_declared_filter_in_operator(self, client_factory, session_factory) -> None:
        svc = ResourceService(SvcFilterableWidget, session_factory=session_factory)
        async with client_factory(svc) as client:
            await client.post("/svc-filterable-widgets", json={"label": "a", "size": 1})
            await client.post("/svc-filterable-widgets", json={"label": "b", "size": 2})
            await client.post("/svc-filterable-widgets", json={"label": "c", "size": 3})
            r = await client.get("/svc-filterable-widgets?size__in=1&size__in=3")
            assert r.status_code == 200
            assert len(r.json()["items"]) == 2


# ---------------------------------------------------------------------------
# OpenAPI schema: declared filter fields surface as separate query params (#31)
# ---------------------------------------------------------------------------


def _build_app(resource: type[BaseResource]) -> FastAPI:
    app = FastAPI()
    ResourceService(resource).register(app, session_dependency=lambda: None)
    register_error_handlers(app)
    return app


def _openapi(app: FastAPI) -> dict:
    """Generate the OpenAPI schema, tolerating a pre-existing
    ``PydanticJsonSchemaWarning`` emitted by some resource model fields whose
    defaults (the ``MISSING`` sentinel) are not JSON-serializable. That warning
    is unrelated to the search-route filter params under test here (#31)."""
    import warnings

    from pydantic.json_schema import PydanticJsonSchemaWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PydanticJsonSchemaWarning)
        return app.openapi()


class TestSearchOpenApiSchema:
    """Issues #31 / #30: filter + sort params surface as typed query params in OpenAPI."""

    def test_filter_fields_appear_as_query_params(self) -> None:
        app = _build_app(SvcFilterableWidget)
        params = _openapi(app)["paths"]["/svc-filterable-widgets"]["get"]["parameters"]
        names = {p["name"] for p in params}
        # standard search params are still present (sort is now an enum, plus desc)
        assert {"limit", "cursor", "sort", "desc"} <= names
        # each declared filter field is its own query param (not a single $ref)
        assert {"label__eq", "label__contains", "size__eq", "size__gte", "size__in"} <= names

    def test_list_typed_filter_field_appears_as_array(self) -> None:
        app = _build_app(SvcFilterableWidget)
        params = _openapi(app)["paths"]["/svc-filterable-widgets"]["get"]["parameters"]
        size_in = next(p for p in params if p["name"] == "size__in")
        schema = size_in["schema"]
        # list-typed (``in`` operator) fields must survive as array params, not
        # be silently dropped by FastAPI's model-as-dependency path.
        assert schema["anyOf"][0] == {"type": "array", "items": {"type": "integer"}}

    def test_no_filter_class_means_no_filter_params(self) -> None:
        app = _build_app(SvcWidget)
        params = _openapi(app)["paths"]["/svc-widgets"]["get"]["parameters"]
        names = {p["name"] for p in params}
        assert {"limit", "cursor", "sort", "desc"} <= names
        # no field__op params are advertised when the resource declares no filter
        assert not any("__" in n for n in names)

    def test_filter_param_types_match_declarations(self) -> None:
        app = _build_app(SvcFilterableWidget)
        params = _openapi(app)["paths"]["/svc-filterable-widgets"]["get"]["parameters"]
        by_name = {p["name"]: p for p in params}
        # scalar int filter -> integer
        assert by_name["size__eq"]["schema"]["anyOf"][0] == {"type": "integer"}
        # scalar str filter -> string
        assert by_name["label__contains"]["schema"]["anyOf"][0] == {"type": "string"}

    def test_sort_is_enum_of_sortable_fields(self) -> None:
        # Issue #30: ``sort`` advertises exactly the resource's sortable fields.
        app = _build_app(SvcWidget)
        params = _openapi(app)["paths"]["/svc-widgets"]["get"]["parameters"]
        sort_param = next(p for p in params if p["name"] == "sort")
        # the enum is exposed via a referenced component schema
        ref = sort_param["schema"]["anyOf"][0]["$ref"]
        enum_schema = _openapi(app)["components"]["schemas"][ref.split("/")[-1]]
        assert set(enum_schema["enum"]) == set(SvcWidget.get_sortable_fields())
        # SecretStr-free SvcWidget sorts id/label/size/created_at
        assert {"id", "label", "size", "created_at"} <= set(enum_schema["enum"])

    def test_desc_param_is_boolean_default_false(self) -> None:
        app = _build_app(SvcWidget)
        params = _openapi(app)["paths"]["/svc-widgets"]["get"]["parameters"]
        desc_param = next(p for p in params if p["name"] == "desc")
        assert desc_param["schema"] == {"type": "boolean", "default": False, "title": "Desc"}

    def test_sort_omitted_when_no_sortable_fields(self) -> None:
        # Issue #30: a resource with no sortable fields exposes neither
        # ``sort`` nor ``desc`` on the search endpoint.
        app = _build_app(SvcUnsortableWidget)
        params = _openapi(app)["paths"]["/svc-unsortable-widgets"]["get"]["parameters"]
        names = {p["name"] for p in params}
        assert "sort" not in names
        assert "desc" not in names
        assert {"limit", "cursor"} <= names
        assert SvcUnsortableWidget.get_sortable_fields() == []

    def test_search_response_items_typed_as_read_model(self) -> None:
        """The search endpoint's 200 response references the read model, not bare Any."""
        app = _build_app(SvcWidget)
        schema = _openapi(app)
        response = schema["paths"]["/svc-widgets"]["get"]["responses"]["200"]
        ref = response["content"]["application/json"]["schema"]["$ref"]
        page_name = ref.split("/")[-1]
        page_schema = schema["components"]["schemas"][page_name]
        items = page_schema["properties"]["items"]
        assert items["type"] == "array"
        assert items["items"]["$ref"] == "#/components/schemas/SvcWidgetRead"
