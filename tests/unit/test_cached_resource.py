"""Tests for the read-through caching wrapper and ``CacheStrategy.should_read`` (issue #168).

Two halves:

* the programmatic freshness decision each shipped strategy answers
  (``should_read``): optimistic is a pure time check, last-modified / ETag
  cannot decide without a source contact, the ``core`` placeholder always reads;
* the wrapper resource serves a read from memory when ``should_read`` is
  ``False`` and refills on ``True``, never shares a caller-scoped response, and
  evicts on a write.

A hand-written ``FakeResource`` / ``FakeService`` with an in-memory table and a
call counter is used so a "was the source read?" question is deterministic —
the wrapper's own code paths are the real ones under test.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from resourcey.cache.cache_store import CacheEntry, CacheStore, InMemoryCacheStore
from resourcey.cache.cache_strategy import (
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
    OptimisticCacheStrategy,
)
from resourcey.cache.cached_resource import CachedResource
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import (
    Action,
    CacheStrategy,
    Create,
    Delete,
    NotFoundError,
    Page,
    Service,
    ServiceError,
    Update,
)
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

# ---------------------------------------------------------------------------
# should_read per strategy
# ---------------------------------------------------------------------------


async def test_optimistic_should_read_is_a_time_check():
    strategy = OptimisticCacheStrategy(expire_in=60)
    now = datetime.now(UTC)
    assert await strategy.should_read(now) is False
    assert await strategy.should_read(now - timedelta(seconds=59)) is False
    assert await strategy.should_read(now - timedelta(seconds=61)) is True


async def test_optimistic_should_read_without_read_at_always_reads():
    assert await OptimisticCacheStrategy(expire_in=60).should_read(None) is True


async def test_optimistic_should_read_normalizes_naive_read_at():
    # A naive read_at is assumed UTC rather than crashing the comparison.
    naive = datetime.now(UTC).replace(tzinfo=None)
    assert await OptimisticCacheStrategy(expire_in=60).should_read(naive) is False


async def test_etag_should_read_cannot_avoid_the_source_read():
    strategy = ETagCacheStrategy()
    now = datetime.now(UTC)
    assert await strategy.should_read(now) is True
    assert await strategy.should_read(now, '"abc"') is True
    assert await strategy.should_read(None) is True


async def test_last_modified_should_read_needs_the_source_timestamp():
    strategy = LastModifiedCacheStrategy()
    now = datetime.now(UTC)
    assert await strategy.should_read(now) is True
    assert await strategy.should_read(now, '"abc"') is True


async def test_core_placeholder_should_read_always_reads():
    from resourcey.core.service import CacheStrategy as CoreCacheStrategy

    assert await CoreCacheStrategy().should_read(datetime.now(UTC)) is True


async def test_cache_base_should_read_always_reads():
    from resourcey.cache.cache_strategy import CacheStrategy as CacheBaseStrategy

    class Minimal(CacheBaseStrategy[Any]):
        def get_cache_header(self, models: list[Any], *, context: Any = None) -> Any:
            return None

    assert await Minimal().should_read(datetime.now(UTC)) is True


# ---------------------------------------------------------------------------
# A minimal resource/service with a source-read counter
# ---------------------------------------------------------------------------


class Row(BaseModel):
    id: int
    label: str
    updated_at: datetime | None = None


class FakeService(Service[Row, int]):
    """An in-memory table that counts source reads, so caching is observable."""

    def __init__(self, rows: dict[int, Row]) -> None:
        super().__init__()
        self._rows = rows
        self.read_calls = 0
        self.search_calls = 0
        self.count_calls = 0
        self.batch_read_calls = 0
        self._private = False

    def response_is_private(self) -> bool:
        return self._private

    async def read(self, id: int) -> Row:  # noqa: A002
        self._require_entered()
        self.read_calls += 1
        if id not in self._rows:
            raise NotFoundError(id)
        return self._rows[id]

    async def search(
        self,
        search_filter: SearchFilter[Row] | None = None,
        sort_order: SortOrder[Row] | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Page[Row]:
        self._require_entered()
        self.search_calls += 1
        items = list(self._rows.values())
        return Page(items=items, limit=limit, next_cursor=None)

    async def count(self, search_filter: SearchFilter[Row] | None = None) -> int:
        self._require_entered()
        self.count_calls += 1
        return len(self._rows)

    async def batch_read(self, ids: list[int]) -> list[Row | None]:
        self._require_entered()
        self.batch_read_calls += 1
        return [self._rows.get(i) for i in ids]

    async def create(self, payload: Row) -> Row:
        self._require_entered()
        new_id = max(self._rows, default=0) + 1
        row = payload.model_copy(update={"id": new_id})
        self._rows[new_id] = row
        return row

    async def update(
        self, payload: Row, *, condition: SearchFilter[Any] | None = None
    ) -> Row | None:
        self._require_entered()
        if payload.id not in self._rows:
            return None
        self._rows[payload.id] = payload
        return payload

    async def delete(self, id: int, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        self._require_entered()
        return self._rows.pop(id, None) is not None

    async def batch_edit(
        self, edits: list[Create[Row] | Update[Row] | Delete[int]]
    ) -> list[Row | None]:
        self._require_entered()
        results: list[Row | None] = []
        for edit in edits:
            if isinstance(edit, Create):
                results.append(await self.create(edit.item))
            elif isinstance(edit, Update):
                results.append(await self.update(edit.item))
            else:
                await self.delete(edit.id)
                results.append(None)
        return results


class FakeResource(Resource[Row, int]):
    """A read-only device under test carrying a chosen cache strategy."""

    def __init__(
        self, rows: dict[int, Row] | None = None, strategy: CacheStrategy | None = None
    ) -> None:
        self._rows = rows if rows is not None else {1: Row(id=1, label="a")}
        self._strategy = strategy
        self.service: FakeService | None = None

    def get_dto_type(self) -> type[Row]:
        return Row

    def get_rest_models(self) -> Any:
        raise NotImplementedError

    def get_id_field(self) -> str:
        return "id"

    def get_resource_path(self) -> str:
        return "rows"

    def get_cache_strategy(self) -> CacheStrategy | None:
        return self._strategy

    def get_queryable_fields(self) -> frozenset[str]:
        return frozenset()

    def get_filter_operators(self) -> Any:
        return {}

    def get_search_filter_type(self) -> Any:
        return None

    def get_sortable_fields(self) -> frozenset[str]:
        return frozenset()

    def get_sort_order_type(self) -> Any:
        return None

    def resolve_sort_order(self, sort: str | None, desc: bool) -> Any:
        return None

    def get_supported_actions(self) -> frozenset[Action]:
        return frozenset(Action)

    def get_exposed_resource(self) -> Resource[Row, int]:
        return self

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[Row, int]:
        self.service = FakeService(self._rows)
        return self.service

    def on_register(self, manifest: Any) -> None:
        pass

    def get_manifest(self) -> Any:
        return None

    async def __aenter__(self) -> Resource[Row, int]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def _optimistic(expire_in: int = 600) -> OptimisticCacheStrategy:
    return OptimisticCacheStrategy(expire_in=expire_in, private=True)


async def _enter(resource: CachedResource[Row, int]) -> CachedResource[Row, int]:
    await resource.__aenter__()
    return resource


# ---------------------------------------------------------------------------
# Read-through
# ---------------------------------------------------------------------------


async def test_warm_cache_serves_without_a_second_source_read():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        first = await service.read(1)
        second = await service.read(1)
    assert first == second == Row(id=1, label="a")
    assert inner.service is not None
    assert inner.service.read_calls == 1
    await cached.__aexit__()


async def test_stale_cache_refills_from_the_source():
    inner = FakeResource(strategy=_optimistic())
    store = InMemoryCacheStore()
    cached = await _enter(CachedResource(inner, store=store))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        assert inner.service is not None and inner.service.read_calls == 1
        # Age the entry past the freshness window.
        entry = await store.get("rows:read:1")
        assert entry is not None
        await store.set(
            "rows:read:1",
            entry.model_copy(update={"read_at": datetime.now(UTC) - timedelta(days=1)}),
        )
        await service.read(1)
        assert inner.service.read_calls == 2
    await cached.__aexit__()


async def test_no_strategy_always_reads_through():
    inner = FakeResource(strategy=None)
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        await service.read(1)
        assert inner.service is not None
        assert inner.service.read_calls == 2
    await cached.__aexit__()


async def test_etag_strategy_always_reads_through():
    # An ETag cannot be decided without reading, so the wrapper still reads.
    inner = FakeResource(strategy=ETagCacheStrategy())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        await service.read(1)
        assert inner.service is not None
        assert inner.service.read_calls == 2
    await cached.__aexit__()


async def test_search_count_batch_read_are_cached():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.search()
        await service.search()
        await service.count()
        await service.count()
        await service.batch_read([1])
        await service.batch_read([1])
    assert inner.service is not None
    assert inner.service.search_calls == 1
    assert inner.service.count_calls == 1
    assert inner.service.batch_read_calls == 1
    await cached.__aexit__()


async def test_different_reads_do_not_share_a_cache_key():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        await service.read(1)  # served from cache
        with pytest.raises(NotFoundError):
            await service.read(2)  # a distinct key: a real source read
    assert inner.service is not None
    # id 1 cached after one source read; id 2 is a separate key and a real miss.
    assert inner.service.read_calls == 2
    await cached.__aexit__()


# ---------------------------------------------------------------------------
# Caller-scoped responses are never shared
# ---------------------------------------------------------------------------


async def test_private_wrapper_never_caches():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner, private=True))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        await service.read(1)
        assert inner.service is not None
        assert inner.service.read_calls == 2
    await cached.__aexit__()


async def test_caller_scoped_inner_service_never_caches():
    inner = FakeResource(strategy=_optimistic())

    class PrivateInner(FakeService):
        def response_is_private(self) -> bool:
            return True

    async def get_service(ctx: Any = None) -> Service[Row, int]:
        svc = PrivateInner(inner._rows)
        inner.service = svc
        return svc

    inner.get_service = get_service  # type: ignore[method-assign]

    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        assert service.response_is_private() is True
        await service.read(1)
        await service.read(1)
        assert inner.service is not None
        assert inner.service.read_calls == 2
    await cached.__aexit__()


# ---------------------------------------------------------------------------
# Writes evict
# ---------------------------------------------------------------------------


async def test_write_evicts_the_cached_read():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        assert inner.service is not None and inner.service.read_calls == 1
        await service.create(Row(id=0, label="b"))
        await service.read(1)
        # The write evicted the entry, so the next read hits the source.
        assert inner.service.read_calls == 2
    await cached.__aexit__()


async def test_update_and_delete_evict():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        await service.update(Row(id=1, label="z"))
        await service.read(1)
        assert inner.service is not None
        assert inner.service.read_calls == 2
        await service.delete(1)
        # 1 is gone; reading it errors rather than serving the stale copy.
        with pytest.raises(NotFoundError):
            await service.read(1)
    await cached.__aexit__()


# ---------------------------------------------------------------------------
# Delegation / surface
# ---------------------------------------------------------------------------


async def test_wrapper_delegates_surface_and_exposes_itself():
    inner = FakeResource(strategy=_optimistic())
    cached = CachedResource(inner)
    assert cached.get_exposed_resource() is cached
    assert cached.get_resource_path() == "rows"
    assert cached.get_id_field() == "id"
    assert cached.get_supported_actions() == frozenset(Action)
    assert cached.get_cache_strategy() is inner.get_cache_strategy()


async def test_default_store_is_in_memory():
    cached = CachedResource(FakeResource(strategy=_optimistic()))
    assert isinstance(cached._store, InMemoryCacheStore)


async def test_wrapper_delegates_every_surface_method():
    inner = FakeResource(strategy=_optimistic())
    cached = CachedResource(inner)
    assert cached.get_dto_type() is Row
    assert cached.get_resource_path() == inner.get_resource_path()
    assert cached.get_id_field() == inner.get_id_field()
    assert cached.get_queryable_fields() == inner.get_queryable_fields()
    assert cached.get_filter_operators() == inner.get_filter_operators()
    assert cached.get_search_filter_type() is inner.get_search_filter_type()
    assert cached.get_sortable_fields() == inner.get_sortable_fields()
    assert cached.get_sort_order_type() is inner.get_sort_order_type()
    assert cached.resolve_sort_order(None, False) == inner.resolve_sort_order(None, False)
    assert cached.get_supported_actions() == inner.get_supported_actions()


async def test_wrapper_delegates_registration_and_lifecycle():
    inner = FakeResource(strategy=_optimistic())
    cached = CachedResource(inner)
    manifest = object()
    cached.on_register(manifest)
    assert inner.get_manifest() is cached.get_manifest()
    assert isinstance(cached._store, InMemoryCacheStore)
    await cached.__aenter__()
    with pytest.raises(ServiceError):
        await cached.__aenter__()
    await cached.__aexit__()


async def test_wrapper_serialization_context_delegates():
    inner = FakeResource(strategy=_optimistic())

    class Secrety(FakeService):
        def serialization_context(self) -> dict[str, Any]:
            return {"expose_secrets": True}

    async def get_service(ctx: Any = None) -> Service[Row, int]:
        svc = Secrety(inner._rows)
        inner.service = svc
        return svc

    inner.get_service = get_service  # type: ignore[method-assign]

    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        assert service.serialization_context() == {"expose_secrets": True}
    await cached.__aexit__()


async def test_batch_edit_evicts_and_forwards():
    inner = FakeResource(strategy=_optimistic())
    cached = await _enter(CachedResource(inner))
    service = await cached.get_service()
    async with service:
        await service.read(1)
        assert inner.service is not None and inner.service.read_calls == 1
        results = await service.batch_edit([Create(item=Row(id=0, label="c"))])
        assert results[0] is not None
        await service.read(1)
        assert inner.service.read_calls == 2
    await cached.__aexit__()


# ---------------------------------------------------------------------------
# CacheStore
# ---------------------------------------------------------------------------


async def test_in_memory_store_round_trips_and_clears_by_prefix():
    store = InMemoryCacheStore()
    entry = CacheEntry(payload={"x": 1}, read_at=datetime.now(UTC))
    await store.set("rows:read:1", entry)
    await store.set("rows:count:z", CacheEntry(payload=3, read_at=datetime.now(UTC)))
    await store.set("other:read:1", CacheEntry(payload="y", read_at=datetime.now(UTC)))
    assert (await store.get("rows:read:1")) is not None
    await store.delete("rows:read:1")
    assert (await store.get("rows:read:1")) is None
    # delete of an absent key is a no-op.
    await store.delete("rows:read:1")
    await store.clear("rows:")
    assert (await store.get("rows:count:z")) is None
    assert (await store.get("other:read:1")) is not None
    await store.clear()
    assert (await store.get("other:read:1")) is None


async def test_cache_store_is_a_discriminated_union():
    store: CacheStore = InMemoryCacheStore()
    dumped = store.model_dump()
    assert dumped["kind"] == "InMemoryCacheStore"
    restored = CacheStore.model_validate(dumped)
    assert isinstance(restored, InMemoryCacheStore)


async def test_resourcey_config_error_is_importable():
    # A trivial guard that the module's import surface is honest.
    assert issubclass(ResourceyConfigError, Exception)
