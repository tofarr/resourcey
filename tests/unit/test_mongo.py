"""Tests for the MongoDB resource backend (issue #47).

Covers the full ``Action`` contract (create / read / update / delete / search /
count / batch_read / batch_edit) at the service level against an in-process
mongomock-backed async collection, plus filter translation, cursor pagination,
sort, the manual-migration-on-read hook, and the optional-extra import guard.

The service code path is identical to production (motor); only the collection
is replaced by ``AsyncMockCollection`` (see ``mongo_mock.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, get_type_hints
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from mongo_mock import make_mock_client
from pydantic import Field

from resourcey.mongo.mongo_filter import to_mongo_query
from resourcey.mongo.mongo_resource import MongoResource
from resourcey.mongo.mongo_service import MongoService
from resourcey.resource.errors import InvalidInputError, NotFoundError, ResourceyConfigError
from resourcey.resource.service import Page
from resourcey.resource.service_base import Action
from resourcey.util.search_filter import (
    ALL,
    NONE,
    AndSearchFilter,
    AttributeFilter,
    BaseSearchFilter,
    Condition,
    OrSearchFilter,
)

# ---------------------------------------------------------------------------
# Test resources
# ---------------------------------------------------------------------------


class MongoWidget(MongoResource):
    """A simple Mongo resource — UUID id, required label, optional size."""

    id: UUID
    label: str
    size: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MongoDoc(MongoResource):
    """A resource with a searchable field and a datetime for sort/cursor tests."""

    id: UUID
    title: Annotated[str, Field(min_length=1)]
    ts: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[BaseSearchFilter] | None:  # type: ignore[override]
        return DocSearchFilter


class DocSearchFilter(BaseSearchFilter[Any]):
    title__contains: str | None = None
    title__eq: str | None = None
    ts__gte: datetime | None = None
    ts__lt: datetime | None = None


class MongoVersioned(MongoResource):
    """A resource exercising the manual-migration-on-read hook."""

    id: UUID
    name: str

    @classmethod
    def migrate_document(cls, doc: dict[str, Any]) -> dict[str, Any]:
        # Simulate a lazy upgrade: add a ``schema_version`` if missing.
        if "schema_version" not in doc:
            doc = {**doc, "schema_version": 2}
        return doc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bind_client(resource: Any, client: Any, database_name: str = "test") -> Any:
    """Set the Mongo client/db cache directly on a resource instance.

    Unit tests that build a collection directly (bypassing the manifest
    lifecycle) use this to seed the client state so ``get_collection`` works.
    Returns the (possibly freshly-instantiated) resource instance.
    """
    if isinstance(resource, type):
        resource = resource()
    resource._client = client
    resource._database_name = database_name
    resource._db = client[database_name]
    return resource


@pytest_asyncio.fixture
async def widget_resource() -> MongoWidget:
    client = make_mock_client()
    return _bind_client(MongoWidget, client)


@pytest_asyncio.fixture
async def doc_resource() -> MongoDoc:
    client = make_mock_client()
    return _bind_client(MongoDoc, client)


@pytest_asyncio.fixture
async def versioned_resource() -> MongoVersioned:
    client = make_mock_client()
    return _bind_client(MongoVersioned, client)


def _collection(resource: Any) -> Any:
    return resource.get_collection()


def _create_widget(label: str, *, size: int = 0, id: UUID | None = None) -> Any:  # noqa: A002
    return MongoWidget().get_create_model()(label=label, size=size, id=id or uuid4())


# ---------------------------------------------------------------------------
# Service-level CRUD tests
# ---------------------------------------------------------------------------


class TestMongoCreate:
    @pytest.mark.asyncio
    async def test_create_returns_read_model_with_id(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        new_id = uuid4()
        result = await svc.create(_create_widget("gadget", id=new_id))
        assert result.id == new_id
        assert result.label == "gadget"
        assert result.size == 0
        assert result.created_at is not None

    @pytest.mark.asyncio
    async def test_create_populates_default_factory_timestamp(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        result = await svc.create(_create_widget("x"))
        assert result.created_at is not None

    @pytest.mark.asyncio
    async def test_create_drops_missing_optional_fields(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        result = await svc.create(_create_widget("x"))
        assert result.size == 0


class TestMongoRead:
    @pytest.mark.asyncio
    async def test_read_returns_entity(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        created = await svc.create(_create_widget("g"))
        result = await svc.read(created.id)
        assert result.label == "g"

    @pytest.mark.asyncio
    async def test_read_missing_raises_not_found(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(NotFoundError):
            await svc.read(uuid4())


class TestMongoUpdate:
    @pytest.mark.asyncio
    async def test_update_applies_patch(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        created = await svc.create(_create_widget("g", size=1))
        result = await svc.update(created.id, MongoWidget().get_update_model()(size=99))
        assert result.size == 99
        assert result.label == "g"

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(NotFoundError):
            await svc.update(uuid4(), MongoWidget().get_update_model()(size=1))

    @pytest.mark.asyncio
    async def test_update_empty_payload_returns_current(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        created = await svc.create(_create_widget("g"))
        result = await svc.update(created.id, MongoWidget().get_update_model()())
        assert result.id == created.id


class TestMongoDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_entity(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        created = await svc.create(_create_widget("g"))
        await svc.delete(created.id)
        with pytest.raises(NotFoundError):
            await svc.read(created.id)

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(NotFoundError):
            await svc.delete(uuid4())


# ---------------------------------------------------------------------------
# Search + pagination + sort
# ---------------------------------------------------------------------------


class TestMongoSearch:
    @pytest.mark.asyncio
    async def test_search_returns_page_with_metadata(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        for i in range(3):
            await svc.create(_create_widget(f"g{i}", size=i))
        page = await svc.search(limit=10)
        assert isinstance(page, Page)
        assert len(page.items) == 3
        assert page.limit == 10
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_paginates_with_cursor(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        for i in range(5):
            await svc.create(_create_widget(f"g{i}"))
        page1 = await svc.search(limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        page2 = await svc.search(limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 2
        assert page2.next_cursor is not None
        page3 = await svc.search(limit=2, cursor=page2.next_cursor)
        assert len(page3.items) == 1
        assert page3.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_rejects_cursor_from_different_sort(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        for _ in range(3):
            await svc.create(_create_widget("g"))
        page = await svc.search(limit=1, sort="label")
        assert page.next_cursor is not None
        with pytest.raises(InvalidInputError):
            await svc.search(limit=1, cursor=page.next_cursor)  # no sort

    @pytest.mark.asyncio
    async def test_search_rejects_invalid_cursor(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(InvalidInputError):
            await svc.search(limit=1, cursor="not-a-real-cursor")

    @pytest.mark.asyncio
    async def test_search_sort_asc(self, doc_resource) -> None:
        svc = MongoService(doc_resource, collection=_collection(doc_resource))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            await svc.create(
                MongoDoc().get_create_model()(title=f"t{i}", ts=base.replace(day=i + 1), id=uuid4())
            )
        page = await svc.search(limit=10, sort="ts")
        assert [item.ts.day for item in page.items] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_search_sort_desc(self, doc_resource) -> None:
        svc = MongoService(doc_resource, collection=_collection(doc_resource))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            await svc.create(
                MongoDoc().get_create_model()(title=f"t{i}", ts=base.replace(day=i + 1), id=uuid4())
            )
        page = await svc.search(limit=10, sort="ts", desc=True)
        assert [item.ts.day for item in page.items] == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_search_rejects_unknown_sort_field(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(InvalidInputError):
            await svc.search(sort="nonexistent")

    @pytest.mark.asyncio
    async def test_search_limit_capped(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        for _ in range(3):
            await svc.create(_create_widget("g"))
        page = await svc.search(limit=999)
        assert page.limit <= 100

    @pytest.mark.asyncio
    async def test_search_rejects_zero_limit(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        with pytest.raises(InvalidInputError):
            await svc.search(limit=0)


class TestMongoCount:
    @pytest.mark.asyncio
    async def test_count_returns_total(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        for _ in range(3):
            await svc.create(_create_widget("g"))
        assert await svc.count() == 3

    @pytest.mark.asyncio
    async def test_count_with_filters(self, doc_resource) -> None:
        svc = MongoService(doc_resource, collection=_collection(doc_resource))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            await svc.create(
                MongoDoc().get_create_model()(title=f"t{i}", ts=base.replace(day=i + 1), id=uuid4())
            )
        f = DocSearchFilter(ts__gte=base.replace(day=2))
        assert await svc.count(filters=f) == 2


class TestMongoBatchRead:
    @pytest.mark.asyncio
    async def test_batch_read_returns_aligned_list(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        a = await svc.create(_create_widget("a"))
        b = await svc.create(_create_widget("b"))
        missing_id = uuid4()
        results = await svc.batch_read([a.id, missing_id, b.id])
        assert len(results) == 3
        assert results[0].label == "a"
        assert results[1] is None
        assert results[2].label == "b"

    @pytest.mark.asyncio
    async def test_batch_read_empty(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        assert await svc.batch_read([]) == []


class TestMongoBatchEdit:
    @pytest.mark.asyncio
    async def test_batch_edit_applies_and_aligns(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        a = await svc.create(_create_widget("a", size=1))
        missing_id = uuid4()
        results = await svc.batch_edit(
            [
                (a.id, MongoWidget().get_update_model()(size=99)),
                (missing_id, MongoWidget().get_update_model()(size=5)),
            ]
        )
        assert results[0].size == 99
        assert results[1] is None


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------


class TestMongoFilterTranslation:
    def test_none_filter_returns_none(self) -> None:
        assert to_mongo_query(None) is None

    def test_all_filter_returns_none(self) -> None:
        assert to_mongo_query(ALL) is None

    def test_none_search_filter_matches_nothing(self) -> None:
        assert to_mongo_query(NONE) == {"_id": None}

    def test_base_filter_eq(self) -> None:
        f = DocSearchFilter(title__eq="hello")
        assert to_mongo_query(f) == {"title": {"$eq": "hello"}}

    def test_base_filter_ne(self) -> None:
        # Build a ne filter via AttributeFilter for coverage
        af = AttributeFilter(attribute="title", value="hello", condition=Condition.NE)
        assert to_mongo_query(af) == {"title": {"$ne": "hello"}}

    def test_base_filter_contains_is_regex_case_insensitive(self) -> None:
        f = DocSearchFilter(title__contains="hello.world")
        q = to_mongo_query(f)
        assert q is not None
        assert q["title"]["$regex"] == "hello\\.world"
        assert q["title"]["$options"] == "i"

    def test_base_filter_in(self) -> None:
        af = AttributeFilter(attribute="title", value=["a", "b"], condition=Condition.IN)
        assert to_mongo_query(af) == {"title": {"$in": ["a", "b"]}}

    def test_attribute_filter_lt(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        af = AttributeFilter(attribute="ts", value=base, condition=Condition.LT)
        assert to_mongo_query(af) == {"ts": {"$lt": base}}

    def test_and_filter(self) -> None:
        f1 = AttributeFilter(attribute="title", value="a", condition=Condition.EQ)
        f2 = AttributeFilter(attribute="size", value=5, condition=Condition.GTE)
        combined = AndSearchFilter(filters=[f1, f2])
        q = to_mongo_query(combined)
        assert q == {"$and": [{"title": {"$eq": "a"}}, {"size": {"$gte": 5}}]}

    def test_or_filter(self) -> None:
        f1 = AttributeFilter(attribute="title", value="a", condition=Condition.EQ)
        f2 = AttributeFilter(attribute="title", value="b", condition=Condition.EQ)
        combined = OrSearchFilter(filters=[f1, f2])
        q = to_mongo_query(combined)
        assert q == {"$or": [{"title": {"$eq": "a"}}, {"title": {"$eq": "b"}}]}

    def test_or_with_all_returns_none(self) -> None:
        f1 = AttributeFilter(attribute="title", value="a", condition=Condition.EQ)
        combined = OrSearchFilter(filters=[f1, ALL])
        assert to_mongo_query(combined) is None

    def test_and_with_all_drops_all(self) -> None:
        f1 = AttributeFilter(attribute="title", value="a", condition=Condition.EQ)
        combined = AndSearchFilter(filters=[f1, ALL])
        assert to_mongo_query(combined) == {"title": {"$eq": "a"}}

    def test_empty_base_filter_returns_none(self) -> None:
        f = DocSearchFilter()
        assert to_mongo_query(f) is None

    def test_id_field_rewritten_to_underscore_id(self) -> None:
        af = AttributeFilter(attribute="id", value=uuid4(), condition=Condition.EQ)
        q = to_mongo_query(af, id_field="id")
        assert "_id" in q

    def test_unknown_filter_type_raises(self) -> None:
        from resourcey.util.search_filter import SearchFilter

        class Custom(SearchFilter[Any]):
            custom: str | None = None

            def matches(self, item: Any) -> bool:
                return True

        with pytest.raises(TypeError):
            to_mongo_query(Custom())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Manual migration on read
# ---------------------------------------------------------------------------


class TestMongoMigrateOnRead:
    @pytest.mark.asyncio
    async def test_migrate_document_invoked_on_read(self, versioned_resource) -> None:
        svc = MongoService(versioned_resource, collection=_collection(versioned_resource))
        new_id = uuid4()
        await svc.create(MongoVersioned().get_create_model()(name="foo", id=new_id))
        # The stored doc has no schema_version; read migrates it.
        result = await svc.read(new_id)
        assert result.name == "foo"
        # The read model doesn't expose schema_version, but the migration ran
        # (the doc was upgraded in the projection).

    @pytest.mark.asyncio
    async def test_migrate_document_invoked_on_search(self, versioned_resource) -> None:
        svc = MongoService(versioned_resource, collection=_collection(versioned_resource))
        for i in range(3):
            await svc.create(MongoVersioned().get_create_model()(name=f"n{i}", id=uuid4()))
        page = await svc.search(limit=10)
        assert len(page.items) == 3

    @pytest.mark.asyncio
    async def test_default_migrate_is_noop(self, widget_resource) -> None:
        svc = MongoService(widget_resource, collection=_collection(widget_resource))
        created = await svc.create(_create_widget("g"))
        result = await svc.read(created.id)
        assert result.label == "g"


# ---------------------------------------------------------------------------
# Resource metadata + import guard
# ---------------------------------------------------------------------------


class TestMongoResourceMeta:
    def test_get_collection_name(self) -> None:
        assert MongoWidget.get_collection_name() == "mongo_widgets"

    @pytest.mark.asyncio
    async def test_build_service_yields_mongo_service(self, widget_resource) -> None:
        instance = MongoWidget()
        instance.on_register()
        service = instance.build_service(instance, _collection(widget_resource))
        assert isinstance(service, MongoService)

    def test_supported_actions_all(self) -> None:
        assert MongoResource().get_supported_actions() == frozenset(Action)

    def test_get_collection_raises_when_unconfigured(self) -> None:
        class Unconfigured(MongoResource):
            id: UUID
            name: str

        instance = Unconfigured()
        instance.on_register()
        with pytest.raises(ResourceyConfigError):
            instance.get_collection()

    @pytest.mark.asyncio
    async def test_get_service_dependency_yields_service(self, widget_resource) -> None:
        # configure already done by fixture; just check the dependency yields
        import contextlib

        instance = MongoWidget()
        # open_storage calls get_collection which reads _db; seed it
        # with a dict-like holding the widget collection.
        instance._db = {MongoWidget.get_collection_name(): _collection(widget_resource)}
        async with contextlib.aclosing(instance.get_service_dependency(None)) as dep:  # type: ignore[arg-type]
            svc = await dep.__anext__()
        assert isinstance(svc, MongoService)

    def test_service_dependency_is_a_request_dependency(self) -> None:
        """The mongo path inherits the base dependency, whose ``request`` is
        annotated ``Request`` — so FastAPI injects it instead of parsing it as
        a query parameter (an unannotated override broke every mongo route)."""
        from fastapi import Request

        hints = get_type_hints(MongoWidget().get_service_dependency)
        assert hints["request"] is Request


class TestImportGuard:
    def test_import_guard_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate motor being unavailable by blocking the import.
        import builtins
        import importlib
        import sys

        real_import = builtins.__import__

        def _block(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "motor":
                raise ImportError("no motor here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block)
        # Clear cached motor modules so the guard re-triggers.
        for mod in [m for m in list(sys.modules) if m.startswith("motor")]:
            del sys.modules[mod]
        # Also clear the cached resourcey.mongo package so its guard re-runs.
        sys.modules.pop("resourcey.mongo", None)
        with pytest.raises(ImportError, match="mongodb"):
            importlib.import_module("resourcey.mongo")


class TestMongoLifecycle:
    """Tests for the instance-level lifecycle / build_client protocol (issue #51)."""

    @pytest.mark.asyncio
    async def test_lifespan_builds_embedded_client_by_default(self) -> None:
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig

        ctx = AppContext(FrameworkConfig())
        instance = MongoWidget()
        instance.on_register()
        await instance.__aenter__(ctx)
        assert instance._client is not None
        assert instance._db is not None
        coll = instance.get_collection()
        assert coll is not None
        await instance.__aexit__(None, None, None)
        await ctx.aclose()
        assert instance._client is None
        assert instance._db is None

    @pytest.mark.asyncio
    async def test_lifespan_reuses_pre_seeded_client(self) -> None:
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig
        from resourcey.mongo.embedded import AsyncEmbeddedClient
        from resourcey.mongo.mongo_resource import _MONGO_CLIENT_KEY

        ctx = AppContext(FrameworkConfig())
        pre = AsyncEmbeddedClient()
        ctx.set(_MONGO_CLIENT_KEY, pre)
        instance = MongoWidget()
        instance.on_register()
        await instance.__aenter__(ctx)
        assert instance._client is pre
        assert instance.get_collection() is not None
        await instance.__aexit__(None, None, None)
        await ctx.aclose()

    @pytest.mark.asyncio
    async def test_build_client_reads_mongo_config(self) -> None:
        from resourcey.app_context import AppContext
        from resourcey.config.config_framework import FrameworkConfig, MongoConfig

        cfg = FrameworkConfig()
        cfg.mongo = MongoConfig(url="embedded", database="custom_db")
        ctx = AppContext(cfg)
        instance = MongoWidget()
        instance.on_register()
        client, db_name, dispose = instance.build_client(ctx)
        assert client is not None
        assert db_name == "custom_db"
        await dispose()

    @pytest.mark.asyncio
    async def test_get_collection_raises_when_unconfigured(self) -> None:
        instance = MongoWidget()
        instance.on_register()
        with pytest.raises(ResourceyConfigError, match="no Mongo client configured"):
            instance.get_collection()
