"""Tests for ``v2`` caching (issue #92): the migration of the v1 cache surface.

Covers the ``v2`` value objects and strategies, the default selection on
:class:`~resourcey.v2.sql.resource.SqlResource`, and the HTTP-layer integration:
``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires`` emission and
``304 Not Modified`` conditional-request short-circuit on read / search / count
/ batch-read / batch-edit / create.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import DateTime, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.cache.cache_defaults import default_cache_strategy
from resourcey.v2.cache.cache_header import CacheHeader
from resourcey.v2.cache.cache_strategy import (
    CacheStrategy,
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
    OptimisticCacheStrategy,
)
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.sql.resource import SqlResource


class Item(BaseModel):
    id: int
    label: str
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# CacheHeader.is_modified matrix
# ---------------------------------------------------------------------------


def test_etag_match_is_not_modified():
    assert CacheHeader(etag='"abc"').is_modified(CacheHeader(etag='"abc"')) is False


def test_etag_mismatch_is_modified():
    assert CacheHeader(etag='"abc"').is_modified(CacheHeader(etag='"def"')) is True


def test_etag_missing_client_validator_is_modified():
    assert CacheHeader(etag='"abc"').is_modified(CacheHeader(etag=None)) is True


def test_etag_list_any_token_matches():
    assert CacheHeader(etag='"abc"').is_modified(CacheHeader(etag='"def", "abc"')) is False


def test_etag_star_matches_any():
    assert CacheHeader(etag='"abc"').is_modified(CacheHeader(etag="*")) is False


def test_updated_at_at_or_before_client_is_not_modified():
    server = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert server.is_modified(CacheHeader(updated_at=datetime(2026, 1, 2, tzinfo=UTC))) is False


def test_updated_at_after_client_is_modified():
    server = CacheHeader(updated_at=datetime(2026, 1, 2, tzinfo=UTC))
    assert server.is_modified(CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))) is True


def test_no_validators_always_modified():
    assert CacheHeader().is_modified(CacheHeader(etag='"x"')) is True


def test_has_any():
    assert CacheHeader().has_any() is False
    assert CacheHeader(etag='"x"').has_any() is True
    assert CacheHeader(expire_at=datetime.now(UTC)).has_any() is True


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def test_etag_strategy_is_stable_and_input_sensitive():
    a = ETagCacheStrategy().get_cache_header([Item(id=1, label="x")])
    b = ETagCacheStrategy().get_cache_header([Item(id=1, label="x")])
    c = ETagCacheStrategy().get_cache_header([Item(id=1, label="y")])
    assert a.etag == b.etag
    assert a.etag != c.etag
    assert a.etag is not None and a.etag.startswith('"') and a.etag.endswith('"')


def test_etag_strategy_skips_none_and_separates_items():
    # A batch result may carry positional ``None`` gaps; they hold no state.
    a = ETagCacheStrategy().get_cache_header([Item(id=1, label="x"), None])
    b = ETagCacheStrategy().get_cache_header([Item(id=1, label="x")])
    assert a.etag == b.etag


def test_last_modified_uses_max_updated_at():
    items = [
        Item(id=1, label="x", updated_at=datetime(2026, 1, 1, tzinfo=UTC)),
        Item(id=2, label="y", updated_at=datetime(2026, 1, 5, tzinfo=UTC)),
    ]
    header = LastModifiedCacheStrategy().get_cache_header(items)
    assert header.updated_at == datetime(2026, 1, 5, tzinfo=UTC)
    assert header.etag is None


def test_last_modified_normalizes_naive_datetimes():
    header = LastModifiedCacheStrategy().get_cache_header(
        [Item(id=1, label="x", updated_at=datetime(2025, 6, 1))]
    )
    assert header.updated_at is not None
    assert header.updated_at.tzinfo is not None


def test_optimistic_produces_only_freshness():
    header = OptimisticCacheStrategy(expire_in=60).get_cache_header([Item(id=1, label="x")])
    assert header.expire_at is not None
    assert header.etag is None and header.updated_at is None


def test_optimistic_requires_positive_expire_in():
    with pytest.raises(ValueError, match="expire_in > 0"):
        OptimisticCacheStrategy(expire_in=0)


def test_negative_expire_in_rejected_on_base():
    with pytest.raises(ValueError, match=">= 0"):
        ETagCacheStrategy(expire_in=-1)


def test_expire_in_sets_expire_at():
    header = ETagCacheStrategy(expire_in=30).get_cache_header([Item(id=1, label="x")])
    assert header.expire_at is not None


def test_count_cache_header_varies_with_count():
    strategy = ETagCacheStrategy()
    assert strategy.count_cache_header(3).etag != strategy.count_cache_header(4).etag
    assert strategy.count_cache_header(3).etag == strategy.count_cache_header(3).etag


def test_count_cache_header_honours_expire_in():
    header = ETagCacheStrategy(expire_in=15).count_cache_header(0)
    assert header.expire_at is not None


def test_strategy_satisfies_the_core_placeholder_seam():
    # ``v2/core`` names the concept; the concrete base extends it so a strategy
    # is usable through the core-level ``get_cache_header`` / ``count_cache_header``.
    from resourcey.v2.core.service import CacheStrategy as CoreCacheStrategy

    strategy = ETagCacheStrategy()
    assert isinstance(strategy, CoreCacheStrategy)
    assert strategy.get_cache_header([Item(id=1, label="x")]).etag is not None
    assert strategy.count_cache_header(0).etag is not None


def test_strategy_round_trips_as_a_discriminated_union():
    strategy: CacheStrategy[Any] = LastModifiedCacheStrategy(expire_in=5)
    dumped = strategy.model_dump()
    assert dumped["kind"] == "LastModifiedCacheStrategy"
    restored = CacheStrategy.model_validate(dumped)
    assert isinstance(restored, LastModifiedCacheStrategy)
    assert restored.expire_in == 5


# ---------------------------------------------------------------------------
# Default selection
# ---------------------------------------------------------------------------


class CacheBase(DeclarativeBase):
    pass


class NoUpdated(CacheBase):
    __tablename__ = "no_updated"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(50))


class HasUpdated(CacheBase):
    __tablename__ = "has_updated"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def test_default_is_etag_without_updated_at():
    assert isinstance(
        default_cache_strategy(
            SqlResource(NoUpdated, session_factory=_factory()).get_rest_models()
        ),
        ETagCacheStrategy,
    )


def test_default_is_last_modified_with_updated_at():
    assert isinstance(
        default_cache_strategy(
            SqlResource(HasUpdated, session_factory=_factory()).get_rest_models()
        ),
        LastModifiedCacheStrategy,
    )


# ---------------------------------------------------------------------------
# SqlResource.get_cache_strategy
# ---------------------------------------------------------------------------


def _factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return async_sessionmaker(engine, expire_on_commit=False)


def test_sql_resource_picks_the_default_strategy():
    resource = SqlResource(NoUpdated, session_factory=_factory())
    assert isinstance(resource.get_cache_strategy(), ETagCacheStrategy)


def test_sql_resource_strategy_is_stable_per_instance():
    resource = SqlResource(NoUpdated, session_factory=_factory())
    assert resource.get_cache_strategy() is resource.get_cache_strategy()


def test_sql_resource_override_seam():
    class Optimistic(SqlResource[Any]):
        def get_cache_strategy(self) -> CacheStrategy[Any]:
            return OptimisticCacheStrategy(expire_in=42)

    resource = Optimistic(NoUpdated, session_factory=_factory())
    strategy = resource.get_cache_strategy()
    assert isinstance(strategy, OptimisticCacheStrategy)
    assert strategy.expire_in == 42


def test_two_dtos_from_the_same_class_get_distinct_strategies():
    # One SqlResource class serves many DTOs, so the strategy must be resolved
    # per instance rather than cached on the class.
    maker = _factory()
    no_updated = SqlResource(NoUpdated, session_factory=maker)
    has_updated = SqlResource(HasUpdated, session_factory=maker)
    assert isinstance(no_updated.get_cache_strategy(), ETagCacheStrategy)
    assert isinstance(has_updated.get_cache_strategy(), LastModifiedCacheStrategy)


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------


async def _make_client(manifest: Manifest, app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from resourcey.v2.http.app import create_app

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = SqlResource(NoUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    manifest: Manifest = Manifest(resources=[items])
    async for c in _make_client(manifest, create_app(manifest)):
        yield c
    await engine.dispose()


async def test_read_emits_etag_and_no_cache(client: AsyncClient):
    created = await client.post("/items", json={"label": "x"})
    assert created.status_code == 201
    assert "etag" in created.headers
    rid = created.json()["id"]

    read = await client.get(f"/items/{rid}")
    assert read.status_code == 200
    assert read.headers["etag"] == created.headers["etag"]
    # A validator with no freshness window forces revalidation.
    assert read.headers["cache-control"] == "no-cache"


async def test_read_304_on_matching_etag(client: AsyncClient):
    created = await client.post("/items", json={"label": "x"})
    etag = created.headers["etag"]
    rid = created.json()["id"]

    revalidated = await client.get(f"/items/{rid}", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag


async def test_read_200_on_mismatched_etag(client: AsyncClient):
    created = await client.post("/items", json={"label": "x"})
    rid = created.json()["id"]
    response = await client.get(f"/items/{rid}", headers={"If-None-Match": '"deadbeef"'})
    assert response.status_code == 200


async def test_search_emits_etag_and_304(client: AsyncClient):
    await client.post("/items", json={"label": "a"})
    first = await client.get("/items")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert (await client.get("/items", headers={"If-None-Match": etag})).status_code == 304


async def test_search_etag_changes_when_results_change(client: AsyncClient):
    first = await client.get("/items")
    await client.post("/items", json={"label": "a"})
    second = await client.get("/items")
    assert first.headers["etag"] != second.headers["etag"]


async def test_count_emits_etag_and_304(client: AsyncClient):
    await client.post("/items", json={"label": "a"})
    first = await client.get("/items/count")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert (await client.get("/items/count", headers={"If-None-Match": etag})).status_code == 304


async def test_count_etag_changes_with_count(client: AsyncClient):
    first = await client.get("/items/count")
    await client.post("/items", json={"label": "a"})
    second = await client.get("/items/count")
    assert first.headers["etag"] != second.headers["etag"]


async def test_batch_read_emits_etag_and_304(client: AsyncClient):
    created = (await client.post("/items", json={"label": "a"})).json()
    first = await client.get("/items/batch-read", params={"id": [created["id"]]})
    assert first.status_code == 200
    etag = first.headers["etag"]
    second = await client.get(
        "/items/batch-read",
        params={"id": [created["id"]]},
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304


async def test_batch_edit_emits_etag(client: AsyncClient):
    created = (await client.post("/items", json={"label": "a"})).json()
    edited = await client.post("/items/batch-edit", json=[{"id": created["id"], "label": "b"}])
    assert edited.status_code == 200
    assert "etag" in edited.headers


async def test_validator_only_last_modified_emits_no_cache():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = SqlResource(HasUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        assert "last-modified" in created.headers
        rid = created.json()["id"]
        read = await client.get(f"/items/{rid}")
        assert read.headers["cache-control"] == "no-cache"
        # The Last-Modified value is a valid second-precision HTTP date; sending
        # it back short-circuits to 304.
        revalidated = await client.get(
            f"/items/{rid}", headers={"If-Modified-Since": read.headers["last-modified"]}
        )
        assert revalidated.status_code == 304
    await engine.dispose()


async def test_expiring_strategy_emits_cache_control_and_expires():
    class EtagExpiring(SqlResource[Any]):
        def get_cache_strategy(self) -> CacheStrategy[Any]:
            return ETagCacheStrategy(expire_in=120)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = EtagExpiring(NoUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        rid = created.json()["id"]
        read = await client.get(f"/items/{rid}")
        assert read.headers["cache-control"].startswith("max-age=")
        assert "expires" in read.headers
    await engine.dispose()


async def test_optimistic_strategy_emits_freshness_only():
    class Optimistic(SqlResource[Any]):
        def get_cache_strategy(self) -> CacheStrategy[Any]:
            return OptimisticCacheStrategy(expire_in=60)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = Optimistic(NoUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        rid = created.json()["id"]
        read = await client.get(f"/items/{rid}")
        assert "etag" not in read.headers
        assert "last-modified" not in read.headers
        assert read.headers["cache-control"].startswith("max-age=")
    await engine.dispose()


async def test_no_strategy_emits_no_cache_headers():
    class Uncached(SqlResource[Any]):
        def get_cache_strategy(self) -> Any:
            return None

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = Uncached(NoUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        assert "etag" not in created.headers
        rid = created.json()["id"]
        read = await client.get(f"/items/{rid}")
        assert "etag" not in read.headers
        assert "cache-control" not in read.headers
    await engine.dispose()


async def test_invalid_if_modified_since_is_treated_as_modified():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = SqlResource(HasUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        rid = created.json()["id"]
        response = await client.get(f"/items/{rid}", headers={"If-Modified-Since": "not-a-date"})
        assert response.status_code == 200
    await engine.dispose()


async def test_mutation_routes_still_emit_etag_without_304():
    # Unsafe methods emit the validator but never short-circuit to 304.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = SqlResource(NoUpdated, session_factory=maker, path="/items")
    async with engine.begin() as conn:
        await conn.run_sync(items.metadata.create_all)

    from resourcey.v2.http.app import create_app

    manifest: Manifest = Manifest(resources=[items])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/items", json={"label": "x"})
        etag = created.headers["etag"]
        rid = created.json()["id"]
        updated = await client.patch(
            f"/items/{rid}",
            json={"label": "y"},
            headers={"If-None-Match": etag},
        )
        assert updated.status_code == 200
        assert updated.json()["label"] == "y"
    await engine.dispose()
