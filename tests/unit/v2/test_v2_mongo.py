"""Tests for the ``v2`` Mongo backend (issue #80).

Exercises the full action contract at the service level over an embedded
(``mongomock``) client, plus filter translation (including the negated-form
complement on absent / null fields), sort translation, keyset cursor pagination
and rejection, the migration-on-read hook, the query-surface security gate, the
client manager, indexes, the query-surface gate, and the config.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient

from resourcey.v2.config.config_base import _reset_config_prefix
from resourcey.v2.core.dto import DTO, DtoField
from resourcey.v2.core.errors import (
    ConflictError,
    InvalidInputError,
    ResourceyConfigError,
    UnsupportedFilterError,
)
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.service import (
    Action,
    Create,
    Delete,
    NotFoundError,
    ServiceError,
    Update,
)
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.mongo.mongo_client import (
    MongoClientManager,
    clear_mongo_client_manager_cache,
    get_mongo_client_manager,
)
from resourcey.v2.mongo.mongo_config import MongoConfig, MongoConnectionConfig
from resourcey.v2.mongo.mongo_filter_converter import (
    MongoFilterContext,
    MongoFilterConverter,
)
from resourcey.v2.mongo.mongo_resource import MongoResource
from resourcey.v2.mongo.mongo_sort_converter import (
    MongoSortContext,
    MongoSortConverter,
)
from resourcey.v2.util.search_filter import (
    AllFilter,
    AndFilter,
    AttrFilter,
    ContainsFilter,
    EqFilter,
    GtFilter,
    NoMatchFilter,
    OrFilter,
    SearchFilter,
    and_,
    attr,
    build_filter,
    not_,
    or_,
)
from resourcey.v2.util.sort_order import AttrSortOrder

# ---------------------------------------------------------------------------
# DTO declarations + resources
# ---------------------------------------------------------------------------


class WidgetDTO(DTO):
    """A simple Mongo DTO: UUID id, required label, optional size."""

    id: UUID
    label: str
    size: int = 0
    created_at: datetime


class SortedDTO(DTO):
    """A DTO with a nullable datetime for sort / cursor NULL tests."""

    id: UUID
    title: str
    ts: datetime | None = None


class VersionedDTO(DTO):
    """A DTO exercising the migration-on-read hook."""

    id: UUID
    name: str


class SecretDTO(DTO):
    """A DTO whose ``secret`` is projected away from the read model."""

    id: UUID
    name: str
    secret: str = DtoField(in_read_response=False, in_search_response=False)


class KeyedDTO(DTO, id_field_name="sku"):
    """A DTO with a client-supplied natural key (a duplicate must conflict)."""

    sku: str
    label: str = ""


class IndexedResource(MongoResource[WidgetDTO, UUID]):
    """A resource declaring a secondary index."""

    def get_indexes(self) -> list[dict[str, Any]]:
        return [{"key": [("label", 1)], "name": "label_idx"}]


class MigratingResource(MongoResource[VersionedDTO, UUID]):
    """A resource migrating an old document shape on read."""

    def migrate_document(self, document: dict[str, Any]) -> dict[str, Any]:
        if "name" not in document and "title" in document:
            document = dict(document)
            document["name"] = document.pop("title")
        return document


class IteratingResource(MongoResource[WidgetDTO, UUID]):
    """A resource opting into the in-memory iteration fallback."""

    allow_filter_iteration = True


class ReadOnlyResource(MongoResource[WidgetDTO, UUID]):
    """A resource narrowing its actions so batch create/delete are rejected."""

    def get_supported_actions(self) -> frozenset[Action]:
        return frozenset({Action.READ, Action.SEARCH, Action.UPDATE})


class DeclaredSortResource(MongoResource[WidgetDTO, UUID]):
    """A resource declaring its own :class:`SortOrder` subclass."""

    def get_sort_order_type(self) -> type[AttrSortOrder[Any, Any]]:
        return AttrSortOrder


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-cursors")
        )
    )


def _manager(url: str = "embedded://test") -> MongoClientManager:
    return MongoClientManager(
        MongoConfig(mongo_connections=[MongoConnectionConfig(name="main", url=url)])
    )


async def _resource(
    dto: type[DTO],
    resource_type: type[MongoResource[Any, Any]] = MongoResource,
    *,
    url: str = "embedded://test",
) -> tuple[MongoClientManager, MongoResource[Any, Any]]:
    manager = _manager(url)
    resource = resource_type(dto, client_manager=manager, encryption_service=_encryption())
    return manager, resource


@pytest_asyncio.fixture
async def widgets() -> AsyncIterator[MongoResource[Any, Any]]:
    manager, resource = await _resource(WidgetDTO)
    async with manager, resource:
        yield resource


async def _seed(resource: MongoResource[Any, Any], **values: Any) -> Any:
    async with await resource.get_service() as service:
        return await service.create(resource.get_dto_type()(**values))


class _ClosingClient:
    """An embedded client whose collections refuse use after ``close()``.

    Mirrors ``motor``'s ``Cannot use MongoClient after close`` so a test can
    observe a lifecycle bug that ``mongomock``'s no-op ``close`` would hide.
    ``client[db]`` returns a proxy whose every attribute access re-checks the
    flag before delegating to the real collection.
    """

    def __init__(self) -> None:
        from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

        self._client = AsyncEmbeddedClient()
        self._closed = False

    def __getitem__(self, database: str) -> Any:
        database_proxy = self._client[database]
        client = self

        class _GuardedDatabase:
            def __getitem__(self, name: str) -> Any:
                collection = database_proxy[name]

                class _GuardedCollection:
                    def __getattr__(self, attribute: str) -> Any:
                        if client._closed:
                            raise RuntimeError("Cannot use MongoClient after close")
                        return getattr(collection, attribute)

                return _GuardedCollection()

        return _GuardedDatabase()

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestMongoConfig:
    def test_parses_named_connections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_NAME", "main")
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_URL", "embedded://main_db")
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_1_NAME", "reports")
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_1_URL", "mongodb://h1,h2/reports_db")
        _reset_config_prefix()
        try:
            config = MongoConfig.get_instance()
            assert [c.name for c in config.connections] == ["main", "reports"]
        finally:
            _reset_config_prefix()

    def test_rejects_blank_name(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            MongoConfig(mongo_connections=[MongoConnectionConfig(name="  ")])

    def test_rejects_duplicate_name(self) -> None:
        with pytest.raises(ValueError, match="Duplicate"):
            MongoConfig(
                mongo_connections=[
                    MongoConnectionConfig(name="main"),
                    MongoConnectionConfig(name="main"),
                ]
            )

    def test_embedded_detection_and_database_name(self) -> None:
        embedded = MongoConnectionConfig(name="main", url="embedded://message_board")
        assert embedded.is_embedded is True
        assert embedded.mongo_database_name("fallback") == "message_board"

        bare = MongoConnectionConfig(name="main", url="embedded")
        assert bare.is_embedded is True
        assert bare.mongo_database_name("fallback") == "fallback"

        real = MongoConnectionConfig(name="main", url="mongodb://h1,h2/reports")
        assert real.is_embedded is False
        assert real.mongo_database_name("fallback") == "reports"

        no_path = MongoConnectionConfig(name="main", url="mongodb://localhost")
        assert no_path.mongo_database_name("fallback") == "fallback"

    def test_mongo_password_only_when_set(self) -> None:
        assert MongoConnectionConfig(name="main").mongo_password is None
        with_password = MongoConnectionConfig(name="main", password="s3cret")
        assert with_password.mongo_password == "s3cret"


# ---------------------------------------------------------------------------
# Client manager
# ---------------------------------------------------------------------------


class TestClientManager:
    async def test_serves_the_first_connection_by_default(self) -> None:
        manager = _manager("embedded://first")
        async with manager:
            client, database_name = await manager.get_client()
            assert database_name == "first"
            assert client is not None

    async def test_unknown_name_raises(self) -> None:
        manager = _manager()
        async with manager:
            with pytest.raises(ResourceyConfigError, match="Unknown Mongo connection name"):
                await manager.get_client("nope")

    async def test_empty_connection_list_raises(self) -> None:
        manager = MongoClientManager(MongoConfig(mongo_connections=[]))
        async with manager:
            with pytest.raises(ResourceyConfigError, match="No Mongo connections"):
                await manager.get_client()

    async def test_using_un_entered_manager_raises(self) -> None:
        manager = _manager()
        with pytest.raises(ResourceyConfigError, match="outside its lifecycle"):
            await manager.get_client()

    async def test_clients_are_cached_per_connection(self) -> None:
        manager = _manager()
        async with manager:
            first, _ = await manager.get_client()
            second, _ = await manager.get_client()
            assert first is second

    async def test_exit_clears_the_client_cache(self) -> None:
        manager = _manager()
        async with manager:
            await manager.get_client()
        assert manager._clients == {}

    async def test_re_entry_does_not_reuse_the_closed_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second lifespan must re-resolve, not reuse the closed client's collection.

        ``mongomock.close()`` is a no-op, so the embedded client cannot show the
        bug; this builds a client that, like ``motor``, refuses use after
        ``close``. Without the ``__aexit__`` reset the cached collection would
        still point at the closed client and the first operation would raise.
        """
        monkeypatch.setattr(
            "resourcey.v2.mongo.mongo_client._build_client", lambda connection: _ClosingClient()
        )
        manager, resource = await _resource(WidgetDTO)
        async with manager, resource:
            await _seed(resource, label="first")
        assert resource.collection is None  # dropped on exit, not left on the closed client

        async with manager, resource, await resource.get_service() as service:
            created = await service.create(resource.get_dto_type()(label="second"))
            assert (await service.read(created.id)).label == "second"

    async def test_embedded_url_builds_the_embedded_client(self) -> None:
        from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

        manager = _manager("embedded://x")
        async with manager:
            client, _ = await manager.get_client()
            assert isinstance(client, AsyncEmbeddedClient)

    async def test_process_wide_accessor_and_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_NAME", "main")
        monkeypatch.setenv("APP_MONGO_CONNECTIONS_0_URL", "embedded://proc")
        _reset_config_prefix()
        clear_mongo_client_manager_cache()
        try:
            manager = get_mongo_client_manager()
            assert get_mongo_client_manager() is manager
            clear_mongo_client_manager_cache()
            assert get_mongo_client_manager() is not manager
        finally:
            clear_mongo_client_manager_cache()
            _reset_config_prefix()

    async def test_entered_property(self) -> None:
        manager = _manager()
        assert manager.entered is False
        async with manager:
            assert manager.entered is True
        assert manager.entered is False

    def test_require_motor_names_the_extra_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("motor"):
                raise ImportError("no motor")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from resourcey.v2.mongo.mongo_client import _require_motor

        with pytest.raises(ImportError, match=r"resourcey\[mongodb\]"):
            _require_motor()

    def test_build_client_passes_password_only_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, dict[str, Any]]] = []

        class FakeMotor:
            def __init__(self, url: str, **kwargs: Any) -> None:
                captured.append((url, kwargs))

        monkeypatch.setattr("resourcey.v2.mongo.mongo_client._require_motor", lambda: FakeMotor)
        from resourcey.v2.mongo.mongo_client import _build_client

        _build_client(MongoConnectionConfig(name="main", url="mongodb://h/db"))
        assert captured[-1] == ("mongodb://h/db", {})
        _build_client(MongoConnectionConfig(name="main", url="mongodb://h/db", password="s3"))
        assert captured[-1] == ("mongodb://h/db", {"password": "s3"})


# ---------------------------------------------------------------------------
# Resource surface
# ---------------------------------------------------------------------------


class TestResourceSurface:
    async def test_path_id_and_collection_name(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert resource.get_resource_path() == "widget-dtos"
        assert resource.get_id_field() == "id"
        assert resource.get_collection_name() == "widget_dtos"

    async def test_explicit_path_and_collection_name(self) -> None:
        manager = _manager()
        resource = MongoResource(
            WidgetDTO, client_manager=manager, path="/widgets", collection_name="w"
        )
        assert resource.get_resource_path() == "widgets"
        assert resource.get_collection_name() == "widget_dtos"
        async with manager, resource:
            assert resource.collection._col.name == "w"

    async def test_query_surface_is_the_read_model(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert resource.get_queryable_fields() == frozenset({"id", "label", "size", "created_at"})
        assert resource.get_sortable_fields() == resource.get_queryable_fields()
        operators = resource.get_filter_operators()
        assert operators["label"] >= {"eq", "contains"}
        assert "contains" not in operators["size"]

    async def test_projected_field_is_neither_filterable_nor_sortable(self) -> None:
        _, resource = await _resource(SecretDTO)
        assert "secret" not in resource.get_queryable_fields()
        assert "secret" not in resource.get_sortable_fields()
        with pytest.raises(InvalidInputError):
            resource.resolve_sort_order("secret", False)
        with pytest.raises(UnsupportedFilterError):
            resource.build_filter_context().field_for("secret")

    async def test_resolve_sort_order_validates_the_surface(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert resource.resolve_sort_order(None, False) is None
        assert resource.resolve_sort_order("label", True) == AttrSortOrder(
            attribute="label", descending=True
        )
        with pytest.raises(InvalidInputError, match="non-sortable"):
            resource.resolve_sort_order("bogus", False)

    async def test_mongo_field_maps_the_identifier(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert resource.mongo_field_for("id") == "_id"
        assert resource.mongo_field_for("label") == "label"

    async def test_cache_strategy_is_last_modified_with_updated_at(self) -> None:
        class WithUpdated(DTO):
            id: UUID
            updated_at: datetime

        _, resource = await _resource(WithUpdated)
        assert type(resource.get_cache_strategy()).__name__ == "LastModifiedCacheStrategy"

    async def test_cache_strategy_is_etag_without_timestamps(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert type(resource.get_cache_strategy()).__name__ == "ETagCacheStrategy"

    async def test_get_service_resolves_and_caches_the_collection(self) -> None:
        manager, resource = await _resource(WidgetDTO)
        async with manager:
            async with await resource.get_service():
                pass
            first = resource.collection
            async with await resource.get_service():
                pass
            assert resource.collection is first

    async def test_a_collection_seeded_on_ctx_is_reused(self) -> None:
        from resourcey.v2.core.service import STORAGE_KEY
        from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

        manager, resource = await _resource(WidgetDTO)
        seeded = AsyncEmbeddedClient()["seeded"]["widget_dtos"]
        async with manager:
            service = await resource.get_service({STORAGE_KEY: seeded})
        assert service._collection is seeded
        assert resource.collection is None  # the manager path was never taken

    async def test_explicit_client_wins_over_the_manager(self) -> None:
        from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

        client = AsyncEmbeddedClient()
        resource = MongoResource(
            WidgetDTO, client=client, database_name="direct", encryption_service=_encryption()
        )
        async with await resource.get_service():
            pass
        assert resource.collection._col.database.name == "direct"
        assert resource.collection._col.name == "widget_dtos"

    async def test_get_manifest_is_none_until_registered(self) -> None:
        _, resource = await _resource(WidgetDTO)
        assert resource.get_manifest() is None
        manifest = Manifest(resources=[resource])
        assert resource.get_manifest() is manifest

    async def test_reentering_a_resource_raises(self) -> None:
        manager, resource = await _resource(WidgetDTO)
        async with manager, resource:
            with pytest.raises(ServiceError, match="already entered"):
                await resource.__aenter__()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class TestActions:
    async def test_create_fills_defaults_and_generates_the_id(self, widgets) -> None:
        created = await _seed(widgets, label="hello")
        assert isinstance(created.id, UUID)
        assert created.size == 0
        assert created.created_at is not None

    async def test_read_returns_the_dto(self, widgets) -> None:
        created = await _seed(widgets, label="hello")
        async with await widgets.get_service() as service:
            assert (await service.read(created.id)).label == "hello"

    async def test_read_missing_raises_not_found(self, widgets) -> None:
        async with await widgets.get_service() as service:
            with pytest.raises(NotFoundError):
                await service.read(uuid4())

    async def test_duplicate_identifier_raises_conflict(self) -> None:
        """A duplicate natural key is translated to :class:`ConflictError` (409)."""
        manager, resource = await _resource(KeyedDTO)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(sku="A1", label="one"))
            with pytest.raises(ConflictError, match="Duplicate Key"):
                await service.create(resource.get_dto_type()(sku="A1", label="two"))

    async def test_a_non_duplicate_insert_error_is_not_mislabelled(self) -> None:
        """Only a duplicate key becomes ``ConflictError``; anything else propagates."""
        from resourcey.v2.core.service import STORAGE_KEY

        class ExplodingCollection:
            async def insert_one(self, document: dict[str, Any]) -> Any:
                raise RuntimeError("transport blew up")

        manager, resource = await _resource(WidgetDTO)
        async with manager, resource:
            service = await resource.get_service({STORAGE_KEY: ExplodingCollection()})
            with pytest.raises(RuntimeError, match="transport blew up"):
                await service.create(resource.get_dto_type()(label="x"))

    async def test_update_is_a_partial_merge(self, widgets) -> None:
        created = await _seed(widgets, label="old", size=5)
        payload = widgets.get_dto_type()(id=created.id, label="new")
        async with await widgets.get_service() as service:
            updated = await service.update(payload)
        assert updated.label == "new"
        assert updated.size == 5  # omitted field preserved

    async def test_update_refreshes_updated_at(self) -> None:
        class TimedDTO(DTO):
            id: UUID
            name: str
            updated_at: datetime

        manager, resource = await _resource(TimedDTO)
        async with manager, resource:
            created = await _seed(resource, name="a")
            async with await resource.get_service() as service:
                # Re-read so both sides are the stored (millisecond-truncated)
                # value BSON carries, not the pre-truncation create document.
                before = await service.read(created.id)
                payload = resource.get_dto_type()(id=created.id, name="b")
                updated = await service.update(payload)
            assert updated.updated_at >= before.updated_at

    async def test_update_requires_the_identifier(self, widgets) -> None:
        async with await widgets.get_service() as service:
            with pytest.raises(ServiceError, match="requires the identifier"):
                await service.update(widgets.get_dto_type()(label="x"))

    async def test_update_missing_raises_not_found(self, widgets) -> None:
        payload = widgets.get_dto_type()(id=uuid4(), label="x")
        async with await widgets.get_service() as service:
            with pytest.raises(NotFoundError):
                await service.update(payload)

    async def test_delete_then_read_raises(self, widgets) -> None:
        created = await _seed(widgets, label="bye")
        async with await widgets.get_service() as service:
            await service.delete(created.id)
            with pytest.raises(NotFoundError):
                await service.read(created.id)

    async def test_delete_missing_raises(self, widgets) -> None:
        async with await widgets.get_service() as service:
            with pytest.raises(NotFoundError):
                await service.delete(uuid4())

    async def test_batch_read_is_positionally_aligned(self, widgets) -> None:
        a = await _seed(widgets, label="a")
        b = await _seed(widgets, label="b")
        missing = uuid4()
        async with await widgets.get_service() as service:
            results = await service.batch_read([a.id, missing, b.id])
        assert results[0] is not None and results[0].label == "a"
        assert results[1] is None
        assert results[2] is not None and results[2].label == "b"

    async def test_batch_read_empty(self, widgets) -> None:
        async with await widgets.get_service() as service:
            assert await service.batch_read([]) == []

    async def test_batch_edit_mixes_create_update_delete(self, widgets) -> None:
        existing = await _seed(widgets, label="existing")
        async with await widgets.get_service() as service:
            results = await service.batch_edit(
                [
                    Create(item=widgets.get_dto_type()(label="new")),
                    Update(item=widgets.get_dto_type()(id=existing.id, label="changed")),
                    Delete(id=uuid4()),
                ]
            )
        assert results[0] is not None and results[0].label == "new"
        assert results[1] is not None and results[1].label == "changed"
        assert results[2] is None

    async def test_batch_edit_update_miss_yields_none(self, widgets) -> None:
        async with await widgets.get_service() as service:
            results = await service.batch_edit(
                [Update(item=widgets.get_dto_type()(id=uuid4(), label="x"))]
            )
        assert results == [None]

    async def test_count_all_and_filtered(self, widgets) -> None:
        await _seed(widgets, label="a")
        await _seed(widgets, label="b")
        async with await widgets.get_service() as service:
            assert await service.count() == 2
            assert await service.count(build_filter([("label", "eq", "a")])) == 1

    async def test_uuid_field_round_trips(self) -> None:
        class RefDTO(DTO):
            id: UUID
            ref: UUID

        manager, resource = await _resource(RefDTO)
        async with manager, resource:
            ref = uuid4()
            created = await _seed(resource, ref=ref)
            async with await resource.get_service() as service:
                assert (await service.read(created.id)).ref == ref

    async def test_update_with_only_the_identifier_reads_back(self, widgets) -> None:
        created = await _seed(widgets, label="stays")
        payload = widgets.get_dto_type()(id=created.id)
        async with await widgets.get_service() as service:
            updated = await service.update(payload)
        assert updated.label == "stays"

    async def test_batch_edit_rejects_a_disallowed_create(self) -> None:
        manager, resource = await _resource(WidgetDTO, ReadOnlyResource)
        async with manager, resource, await resource.get_service() as service:
            with pytest.raises(InvalidInputError, match="cannot create"):
                await service.batch_edit([Create(item=resource.get_dto_type()(label="x"))])

    async def test_batch_edit_rejects_a_disallowed_delete(self) -> None:
        manager, resource = await _resource(WidgetDTO, ReadOnlyResource)
        async with manager, resource, await resource.get_service() as service:
            with pytest.raises(InvalidInputError, match="cannot delete"):
                await service.batch_edit([Delete(id=uuid4())])

    async def test_iterated_documents_without_a_filter(self) -> None:
        manager, resource = await _resource(WidgetDTO, IteratingResource)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(label="a"))
            await service.create(resource.get_dto_type()(label="b"))
            # No filter: the fallback still returns every document.
            found = await service.search()
        assert sorted(w.label for w in found.items) == ["a", "b"]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    async def test_eq_contains_and_ordering(self, widgets) -> None:
        await _seed(widgets, label="alpha", size=1)
        await _seed(widgets, label="beta", size=2)
        await _seed(widgets, label="alphabet", size=3)
        async with await widgets.get_service() as service:
            eq = await service.search(search_filter=build_filter([("label", "eq", "alpha")]))
            assert [w.label for w in eq.items] == ["alpha"]
            contains = await service.search(
                search_filter=build_filter([("label", "contains", "alpha")])
            )
            assert sorted(w.label for w in contains.items) == ["alpha", "alphabet"]
            gt = await service.search(search_filter=build_filter([("size", "gt", 2)]))
            assert [w.size for w in gt.items] == [3]

    async def test_contains_escapes_regex_metacharacters(self, widgets) -> None:
        await _seed(widgets, label="a.b")
        await _seed(widgets, label="axb")
        async with await widgets.get_service() as service:
            found = await service.search(search_filter=build_filter([("label", "contains", "a.b")]))
        assert [w.label for w in found.items] == ["a.b"]

    async def test_and_or_not_compose(self, widgets) -> None:
        await _seed(widgets, label="alpha", size=1)
        await _seed(widgets, label="beta", size=2)
        async with await widgets.get_service() as service:
            combined = await service.search(
                search_filter=and_(
                    attr("label", ContainsFilter(value="a")),
                    attr("size", EqFilter(value=1)),
                )
            )
            assert [w.label for w in combined.items] == ["alpha"]
            either = await service.search(
                search_filter=or_(
                    attr("label", EqFilter(value="alpha")),
                    attr("label", EqFilter(value="beta")),
                )
            )
            assert sorted(w.label for w in either.items) == ["alpha", "beta"]
            negated = await service.search(
                search_filter=not_(attr("label", EqFilter(value="alpha")))
            )
            assert [w.label for w in negated.items] == ["beta"]

    async def test_all_and_no_match(self, widgets) -> None:
        await _seed(widgets, label="alpha")
        async with await widgets.get_service() as service:
            assert await service.count(AllFilter()) == 1
            assert await service.count(NoMatchFilter()) == 0
            assert (await service.search(search_filter=NoMatchFilter())).items == []

    async def test_no_match_excludes_a_null_identifier(self) -> None:
        """The match-nothing sentinel must not match a document with a null ``_id``.

        A client-supplied natural key may be null, so ``{"_id": None}`` would
        wrongly match it; ``{"$nor": [{}]}`` matches nothing unconditionally.
        """

        class NullableKeyDTO(DTO, id_field_name="sku"):
            sku: str | None = None
            label: str = ""

        manager, resource = await _resource(NullableKeyDTO)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(sku=None, label="nullkey"))
            await service.create(resource.get_dto_type()(sku="A1", label="keyed"))
            assert await service.count(NoMatchFilter()) == 0
            assert (await service.search(search_filter=NoMatchFilter())).items == []
            assert await service.count(not_(AllFilter())) == 0

    async def test_unknown_field_raises_unsupported(self, widgets) -> None:
        async with await widgets.get_service() as service:
            with pytest.raises(UnsupportedFilterError, match="not a queryable field"):
                await service.search(search_filter=attr("bogus", EqFilter(value=1)))

    async def test_negated_eq_matches_absent_and_null_fields(self) -> None:
        """The exact-complement contract on an absent / null field."""

        class OptDTO(DTO):
            id: UUID
            label: str | None = None

        manager, resource = await _resource(OptDTO)
        async with manager, resource, await resource.get_service() as service:
            # ``label`` is explicitly null on one doc and absent on another.
            await service.create(resource.get_dto_type()(id=uuid4(), label=None))
            await service.create(resource.get_dto_type()(id=uuid4()))
            await service.create(resource.get_dto_type()(id=uuid4(), label="x"))

            positive = await service.search(search_filter=attr("label", EqFilter(value="x")))
            negated = await service.search(search_filter=not_(attr("label", EqFilter(value="x"))))
        assert [w.label for w in positive.items] == ["x"]
        # Both the null and the absent document land in the complement.
        assert len(negated.items) == 2

    async def test_negated_ordering_matches_the_complement(self, widgets) -> None:
        await _seed(widgets, label="a", size=1)
        await _seed(widgets, label="b", size=5)
        await _seed(widgets, label="c", size=9)
        async with await widgets.get_service() as service:
            negated = await service.search(search_filter=not_(attr("size", GtFilter(value=4))))
        assert [w.size for w in negated.items] == [1]

    async def test_unconvertible_filter_raises_by_default(self, widgets) -> None:
        class CustomFilter(SearchFilter[Any]):
            def matches(self, value: Any) -> bool:  # pragma: no cover - never reached
                return True

        async with await widgets.get_service() as service:
            with pytest.raises(UnsupportedFilterError):
                await service.search(search_filter=CustomFilter())

    async def test_allow_filter_iteration_falls_back_to_a_scan(self) -> None:
        class CustomFilter(SearchFilter[Any]):
            def matches(self, value: Any) -> bool:
                return getattr(value, "label", None) == "alpha"

        manager, resource = await _resource(WidgetDTO, IteratingResource)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(label="alpha"))
            await service.create(resource.get_dto_type()(label="beta"))
            found = await service.search(search_filter=CustomFilter())
            assert await service.count(CustomFilter()) == 1
        assert [w.label for w in found.items] == ["alpha"]


class TestFilterConverter:
    def _converter(self) -> MongoFilterConverter:
        return MongoFilterConverter(
            MongoFilterContext(
                fields={"label": "label", "size": "size", "id": "_id"}, id_field="id"
            )
        )

    def test_all_and_no_match(self) -> None:
        converter = self._converter()
        assert converter.condition(AllFilter()) is None
        assert converter.condition(NoMatchFilter()) == {"$nor": [{}]}

    def test_eq_is_a_bare_value(self) -> None:
        converter = self._converter()
        assert converter.condition(attr("label", EqFilter(value="x"))) == {"label": "x"}

    def test_contains_is_a_case_insensitive_regex(self) -> None:
        converter = self._converter()
        query = converter.condition(attr("label", ContainsFilter(value="a.b")))
        assert query == {"label": {"$regex": "a\\.b", "$options": "i"}}

    def test_id_attribute_maps_to_the_id_field(self) -> None:
        converter = self._converter()
        value = uuid4()
        assert converter.condition(attr("id", EqFilter(value=value))) == {"_id": str(value)}

    def test_negated_eq_is_the_query_level_nor(self) -> None:
        converter = self._converter()
        assert converter.negated_condition(attr("label", EqFilter(value="x"))) == {
            "$nor": [{"label": "x"}]
        }

    def test_and_or_composition(self) -> None:
        converter = self._converter()
        both = converter.condition(
            and_(attr("label", EqFilter(value="x")), attr("id", EqFilter(value="y")))
        )
        assert both == {"$and": [{"label": "x"}, {"_id": "y"}]}
        either = converter.condition(
            or_(attr("label", EqFilter(value="x")), attr("label", EqFilter(value="y")))
        )
        assert either == {"$or": [{"label": "x"}, {"label": "y"}]}

    def test_unknown_field_raises(self) -> None:
        converter = self._converter()
        with pytest.raises(UnsupportedFilterError):
            converter.condition(AttrFilter(attribute="bogus", filter=EqFilter(value=1)))

    def test_unregistered_node_raises(self) -> None:
        class CustomFilter(SearchFilter[Any]):
            def matches(self, value: Any) -> bool:  # pragma: no cover
                return True

        converter = self._converter()
        with pytest.raises(UnsupportedFilterError, match="No Mongo conversion"):
            converter.condition(CustomFilter())

    def test_all_ordering_operators(self) -> None:
        from resourcey.v2.util.search_filter import GeFilter, LeFilter, LtFilter

        converter = self._converter()
        assert converter.condition(attr("label", GeFilter(value=1))) == {"label": {"$gte": 1}}
        assert converter.condition(attr("label", LtFilter(value=2))) == {"label": {"$lt": 2}}
        assert converter.condition(attr("label", LeFilter(value=3))) == {"label": {"$lte": 3}}

    def test_negated_ordering_is_the_query_level_nor(self) -> None:
        converter = self._converter()
        assert converter.negated_condition(attr("size", GtFilter(value=4))) == {
            "$nor": [{"size": {"$gt": 4}}]
        }

    def test_not_negation_unwraps_to_the_positive(self) -> None:
        converter = self._converter()
        node = not_(attr("label", EqFilter(value="x")))
        assert converter.negated_condition(node) == {"label": "x"}

    def test_empty_and_or_nodes(self) -> None:
        converter = self._converter()
        assert converter.condition(AndFilter(filters=())) is None
        assert converter.negated_condition(AndFilter(filters=())) == {"$nor": [{}]}
        assert converter.condition(OrFilter(filters=())) == {"$nor": [{}]}
        assert converter.negated_condition(OrFilter(filters=())) is None

    def test_or_with_an_all_child_is_unrestricted(self) -> None:
        converter = self._converter()
        node = OrFilter(filters=(attr("label", EqFilter(value="x")), AllFilter()))
        assert converter.condition(node) is None

    def test_or_negation_with_a_no_match_child_matches_all(self) -> None:
        converter = self._converter()
        node = OrFilter(filters=(NoMatchFilter(), attr("label", EqFilter(value="x"))))
        assert converter.negated_condition(node) is None

    def test_and_negation_combines_into_an_or(self) -> None:
        converter = self._converter()
        node = and_(attr("label", EqFilter(value="x")), attr("id", EqFilter(value="y")))
        assert converter.negated_condition(node) == {
            "$or": [{"$nor": [{"label": "x"}]}, {"$nor": [{"_id": "y"}]}]
        }

    def test_single_child_and_or_unwrap(self) -> None:
        converter = self._converter()
        assert converter.condition(AndFilter(filters=(attr("label", EqFilter(value="x")),))) == {
            "label": "x"
        }
        assert converter.condition(OrFilter(filters=(attr("label", EqFilter(value="x")),))) == {
            "label": "x"
        }

    def test_value_leaf_without_a_bound_field_raises(self) -> None:
        converter = self._converter()
        with pytest.raises(UnsupportedFilterError, match="without a bound field"):
            converter.condition(EqFilter(value=1))
        with pytest.raises(UnsupportedFilterError, match="without a bound field"):
            converter.negated_condition(EqFilter(value=1))

    async def test_resolve_is_a_noop(self) -> None:
        converter = self._converter()
        assert await converter.resolve() is None

    def test_apply_returns_the_condition(self) -> None:
        converter = self._converter()
        assert converter.apply(AllFilter()) is None
        assert converter.apply(attr("label", EqFilter(value="x"))) == {"label": "x"}


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


class TestSorting:
    async def test_sort_ascending_and_descending(self, widgets) -> None:
        for label in ("Cherry", "Apple", "Banana"):
            await _seed(widgets, label=label)
        async with await widgets.get_service() as service:
            ascending = await service.search(sort_order=widgets.resolve_sort_order("label", False))
            descending = await service.search(sort_order=widgets.resolve_sort_order("label", True))
        assert [w.label for w in ascending.items] == ["Apple", "Banana", "Cherry"]
        assert [w.label for w in descending.items] == ["Cherry", "Banana", "Apple"]

    async def test_default_order_is_the_identifier(self, widgets) -> None:
        created = [await _seed(widgets, label=f"w{i}") for i in range(3)]
        ids = sorted(item.id for item in created)
        async with await widgets.get_service() as service:
            page = await service.search()
        assert [w.id for w in page.items] == ids

    async def test_none_sorts_first_ascending_last_descending(self) -> None:
        manager, resource = await _resource(SortedDTO)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(title="b", ts=datetime(2020, 1, 1)))
            await service.create(resource.get_dto_type()(title="none", ts=None))
            await service.create(resource.get_dto_type()(title="a", ts=datetime(2019, 1, 1)))
            ascending = await service.search(sort_order=resource.resolve_sort_order("ts", False))
            descending = await service.search(sort_order=resource.resolve_sort_order("ts", True))
        assert [w.title for w in ascending.items] == ["none", "a", "b"]
        assert [w.title for w in descending.items] == ["b", "a", "none"]

    async def test_sort_converter_appends_the_identifier(self) -> None:
        converter = MongoSortConverter(
            MongoSortContext(fields={"label": "label", "id": "_id"}, id_field="id")
        )
        assert converter.apply(AttrSortOrder(attribute="label")) == [("label", 1), ("_id", 1)]
        assert converter.apply(AttrSortOrder(attribute="label", descending=True)) == [
            ("label", -1),
            ("_id", 1),
        ]

    def test_sort_converter_rejects_an_unknown_field(self) -> None:
        converter = MongoSortConverter(MongoSortContext(fields={"label": "label"}, id_field="id"))
        with pytest.raises(InvalidInputError):
            converter.apply(AttrSortOrder(attribute="bogus"))

    def test_sort_converter_rejects_an_unregistered_node(self) -> None:
        converter = MongoSortConverter(MongoSortContext(fields={"label": "label"}, id_field="id"))
        with pytest.raises(InvalidInputError, match="No Mongo conversion"):
            converter.apply(AllFilter())  # type: ignore[arg-type]

    def test_register_sort_order_overrides_a_handler(self) -> None:
        from resourcey.v2.mongo.mongo_sort_converter import _REGISTRY, register_sort_order

        ctx = MongoSortContext(fields={"label": "label", "id": "_id"}, id_field="id")
        converter = MongoSortConverter(ctx)
        original = _REGISTRY[AttrSortOrder]
        try:
            register_sort_order(AttrSortOrder, lambda _ctx, _node: ("label", -1))
            assert converter.apply(AttrSortOrder(attribute="label")) == [("label", -1), ("_id", 1)]
        finally:
            register_sort_order(AttrSortOrder, original)


class TestEmbeddedAdapter:
    """The embedded adapter mirrors the small slice of motor's API the service uses."""

    async def test_find_with_limit_and_update_one(self) -> None:
        from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

        collection = AsyncEmbeddedClient()["db"]["col"]
        await collection.insert_one({"_id": "a", "n": 1})
        await collection.insert_one({"_id": "b", "n": 2})
        cursor = collection.find({}, limit=1)
        assert len(await cursor.to_list(length=None)) == 1
        result = await collection.update_one({"_id": "a"}, {"$set": {"n": 9}})
        assert result.acknowledged is True
        assert (await collection.find_one({"_id": "a"}))["n"] == 9


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    async def _walk(self, resource: MongoResource[Any, Any], limit: int) -> list[str]:
        seen: list[str] = []
        cursor: str | None = None
        async with await resource.get_service() as service:
            while True:
                page = await service.search(
                    sort_order=resource.resolve_sort_order("label", False),
                    cursor=cursor,
                    limit=limit,
                )
                seen.extend(w.label for w in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        return seen

    async def test_pages_walk_without_gaps_or_repeats(self, widgets) -> None:
        for i in range(7):
            await _seed(widgets, label=f"item-{i:02d}")
        assert await self._walk(widgets, 3) == [f"item-{i:02d}" for i in range(7)]

    async def test_last_page_has_no_next_cursor(self, widgets) -> None:
        await _seed(widgets, label="only")
        async with await widgets.get_service() as service:
            page = await service.search(
                sort_order=widgets.resolve_sort_order("label", False), limit=5
            )
        assert page.next_cursor is None

    async def test_garbage_cursor_is_rejected(self, widgets) -> None:
        async with await widgets.get_service() as service:
            with pytest.raises(InvalidInputError, match="Invalid or tampered"):
                await service.search(cursor="not-a-cursor")

    async def test_tampered_cursor_is_rejected(self, widgets) -> None:
        await _seed(widgets, label="a")
        await _seed(widgets, label="b")
        async with await widgets.get_service() as service:
            page = await service.search(
                sort_order=widgets.resolve_sort_order("label", False), limit=1
            )
        assert page.next_cursor is not None
        segments = page.next_cursor.split(".")
        segments[3] = segments[3] + "x"
        async with await widgets.get_service() as service:
            with pytest.raises(InvalidInputError):
                await service.search(cursor=".".join(segments))

    async def test_cursor_under_a_different_sort_is_rejected(self, widgets) -> None:
        for i in range(3):
            await _seed(widgets, label=f"item-{i}")
        async with await widgets.get_service() as service:
            page = await service.search(
                sort_order=widgets.resolve_sort_order("label", False), limit=1
            )
            assert page.next_cursor is not None
            with pytest.raises(InvalidInputError, match="different sort"):
                await service.search(
                    sort_order=widgets.resolve_sort_order("size", False),
                    cursor=page.next_cursor,
                )
            with pytest.raises(InvalidInputError, match="different sort"):
                await service.search(cursor=page.next_cursor)

    async def test_paging_over_a_nullable_sort_column(self) -> None:
        manager, resource = await _resource(SortedDTO)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(title="n1", ts=None))
            await service.create(resource.get_dto_type()(title="n2", ts=None))
            await service.create(resource.get_dto_type()(title="a", ts=datetime(2019, 1, 1)))
            await service.create(resource.get_dto_type()(title="b", ts=datetime(2020, 1, 1)))
            seen: list[str] = []
            cursor: str | None = None
            while True:
                page = await service.search(
                    sort_order=resource.resolve_sort_order("ts", False),
                    cursor=cursor,
                    limit=1,
                )
                seen.extend(w.title for w in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        # Nulls sort first (tie-broken by the identifier, so the two null rows'
        # relative order is not asserted), then the non-null rows ascending.
        assert set(seen[:2]) == {"n1", "n2"}
        assert seen[2:] == ["a", "b"]

    async def test_next_cursor_is_opaque_and_kid_tagged(self, widgets) -> None:
        for i in range(3):
            await _seed(widgets, label=f"item-{i}")
        async with await widgets.get_service() as service:
            page = await service.search(
                sort_order=widgets.resolve_sort_order("label", False), limit=1
            )
        assert page.next_cursor is not None
        assert page.next_cursor.count(".") == 4
        assert "item-" not in page.next_cursor

    async def test_paging_descending_over_a_nullable_column(self) -> None:
        manager, resource = await _resource(SortedDTO)
        async with manager, resource, await resource.get_service() as service:
            await service.create(resource.get_dto_type()(title="n1", ts=None))
            await service.create(resource.get_dto_type()(title="a", ts=datetime(2019, 1, 1)))
            await service.create(resource.get_dto_type()(title="b", ts=datetime(2020, 1, 1)))
            seen: list[str] = []
            cursor: str | None = None
            while True:
                page = await service.search(
                    sort_order=resource.resolve_sort_order("ts", True),
                    cursor=cursor,
                    limit=1,
                )
                seen.extend(w.title for w in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        # Descending: non-null rows descending, then the null row last.
        assert seen == ["b", "a", "n1"]

    async def test_paging_by_the_identifier_only(self, widgets) -> None:
        created = [await _seed(widgets, label=f"w{i}") for i in range(3)]
        ids = sorted(item.id for item in created)
        seen: list[UUID] = []
        cursor: str | None = None
        async with await widgets.get_service() as service:
            while True:
                page = await service.search(cursor=cursor, limit=2)
                seen.extend(w.id for w in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        assert seen == ids

    async def test_declared_sort_order_type_is_used(self) -> None:
        manager, resource = await _resource(WidgetDTO, DeclaredSortResource)
        async with manager, resource:
            assert resource.get_sort_order_type() is AttrSortOrder
            order = resource.resolve_sort_order("label", True)
            assert isinstance(order, AttrSortOrder)
            assert order.descending is True
            with pytest.raises(InvalidInputError):
                resource.resolve_sort_order("bogus", False)

    async def test_declared_filter_type_defaults_to_none(self, widgets) -> None:
        assert widgets.get_search_filter_type() is None


# ---------------------------------------------------------------------------
# Indexes + migration-on-read
# ---------------------------------------------------------------------------


class TestIndexesAndMigration:
    async def test_ensure_indexes_creates_declared_indexes(self) -> None:
        manager = _manager()
        resource = IndexedResource(
            WidgetDTO, client_manager=manager, encryption_service=_encryption()
        )
        async with manager, resource:
            collection = resource.collection
            assert "label_idx" in collection._col.index_information()

    async def test_default_get_indexes_is_empty(self, widgets) -> None:
        assert widgets.get_indexes() == []

    async def test_migrate_document_runs_on_read(self) -> None:
        manager = _manager()
        resource = MigratingResource(
            VersionedDTO, client_manager=manager, encryption_service=_encryption()
        )
        async with manager, resource:
            # Seed an old-shaped document directly, bypassing the DTO.
            legacy_id = uuid4()
            await resource.collection.insert_one({"_id": str(legacy_id), "title": "legacy"})
            async with await resource.get_service() as service:
                found = await service.read(legacy_id)
        assert found.name == "legacy"

    async def test_default_migrate_document_is_a_noop(self, widgets) -> None:
        document = {"_id": "x", "label": "y"}
        assert widgets.migrate_document(document) is document


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class TestHttpTransport:
    """A Mongo-backed resource served through the shared v2 HTTP assembly."""

    @pytest_asyncio.fixture
    async def client(self) -> AsyncIterator[AsyncClient]:
        from httpx import ASGITransport, AsyncClient

        from resourcey.v2.http.app import create_app

        manager = _manager()
        resource = MongoResource(
            WidgetDTO, client_manager=manager, encryption_service=_encryption()
        )
        manifest = Manifest(resources=[resource], managers=[manager])
        app = create_app(manifest)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                yield http

    async def test_crud_over_http(self, client: AsyncClient) -> None:
        created = await client.post("/widget-dtos", json={"label": "hello"})
        assert created.status_code == 201
        body = created.json()
        assert body["label"] == "hello"
        assert body["size"] == 0
        widget_id = body["id"]

        read = await client.get(f"/widget-dtos/{widget_id}")
        assert read.status_code == 200
        assert read.json()["label"] == "hello"

        patched = await client.patch(f"/widget-dtos/{widget_id}", json={"label": "new"})
        assert patched.status_code == 200
        assert patched.json()["label"] == "new"

        deleted = await client.delete(f"/widget-dtos/{widget_id}")
        assert deleted.status_code in (200, 204)
        assert (await client.get(f"/widget-dtos/{widget_id}")).status_code == 404

    async def test_read_missing_is_404(self, client: AsyncClient) -> None:
        assert (await client.get(f"/widget-dtos/{uuid4()}")).status_code == 404

    async def test_search_count_and_filters(self, client: AsyncClient) -> None:
        await client.post("/widget-dtos", json={"label": "alpha", "size": 1})
        await client.post("/widget-dtos", json={"label": "beta", "size": 2})
        found = await client.get("/widget-dtos", params={"label__eq": "alpha"})
        assert found.status_code == 200
        assert [w["label"] for w in found.json()["items"]] == ["alpha"]
        count = await client.get("/widget-dtos/count", params={"label__eq": "alpha"})
        assert count.status_code == 200

    async def test_unknown_filter_is_400(self, client: AsyncClient) -> None:
        response = await client.get("/widget-dtos", params={"bogus__eq": "x"})
        assert response.status_code == 400

    async def test_sort_and_pagination_over_http(self, client: AsyncClient) -> None:
        for label in ("Cherry", "Apple", "Banana"):
            await client.post("/widget-dtos", json={"label": label})
        page = await client.get("/widget-dtos", params={"sort": "label", "limit": 2})
        assert page.status_code == 200
        body = page.json()
        assert [w["label"] for w in body["items"]] == ["Apple", "Banana"]
        assert body["next_cursor"]
        next_page = await client.get(
            "/widget-dtos",
            params={"sort": "label", "limit": 2, "cursor": body["next_cursor"]},
        )
        assert [w["label"] for w in next_page.json()["items"]] == ["Cherry"]

    async def test_non_sortable_field_is_400(self, client: AsyncClient) -> None:
        assert (await client.get("/widget-dtos", params={"sort": "bogus"})).status_code == 400

    async def test_duplicate_natural_key_is_409(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from resourcey.v2.http.app import create_app

        manager = _manager()
        resource = MongoResource(KeyedDTO, client_manager=manager, encryption_service=_encryption())
        manifest = Manifest(resources=[resource], managers=[manager])
        app = create_app(manifest)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                first = await http.post("/keyed-dtos", json={"sku": "A1", "label": "one"})
                assert first.status_code == 201
                duplicate = await http.post("/keyed-dtos", json={"sku": "A1", "label": "two"})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "conflict"
