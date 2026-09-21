"""Tests for the caching strategy feature (issue #38).

Covers:
- ``CacheHeader`` value object and the ``is_modified`` matrix.
- ``CacheStrategy`` polymorphic base + ``ETagCacheStrategy``,
  ``LastModifiedCacheStrategy``, ``OptimisticCacheStrategy``.
- ``expire_in`` validation (``< 0`` rejected; ``OptimisticCacheStrategy``
  requires ``> 0``).
- ``BaseResource.get_cache_strategy`` default selection (LastModified when
  ``updated_at`` readable, else ETag), caching, and the override seam.
- ``ResourceService.compute_cache_header`` / ``compute_count_cache_header``.
- HTTP-layer integration: ``ETag`` / ``Last-Modified`` / ``Cache-Control`` /
  ``Expires`` emission and ``304 Not Modified`` conditional-request
  short-circuit on read / search / count / batch-read / batch-edit.
- Discriminated-union serialization round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.cache.cache_header import CacheHeader
from resourcey.cache.cache_strategy import (
    CacheStrategy,
    ETagCacheStrategy,
    LastModifiedCacheStrategy,
    OptimisticCacheStrategy,
)
from resourcey.resource.field import ResourceyField
from resourcey.resource.routes import register_error_handlers, register_routes
from resourcey.resource.service import SqlService
from resourcey.resource.sql import ResourceyBase, SqlResource

# ---------------------------------------------------------------------------
# Pydantic read-model fixtures
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int
    label: str
    updated_at: datetime | None = None


class SecretItem(BaseModel):
    id: int
    label: str
    secret: str = "**********"


# ---------------------------------------------------------------------------
# CacheHeader.is_modified matrix
# ---------------------------------------------------------------------------


class TestCacheHeaderIsModified:
    def test_etag_match_is_not_modified(self) -> None:
        h = CacheHeader(etag='"abc"')
        assert h.is_modified(CacheHeader(etag='"abc"')) is False

    def test_etag_mismatch_is_modified(self) -> None:
        h = CacheHeader(etag='"abc"')
        assert h.is_modified(CacheHeader(etag='"def"')) is True

    def test_etag_set_client_missing_etag_is_modified(self) -> None:
        h = CacheHeader(etag='"abc"')
        assert h.is_modified(CacheHeader(etag=None)) is True

    def test_updated_at_le_client_is_not_modified(self) -> None:
        server = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        client = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert server.is_modified(client) is False

    def test_updated_at_before_client_is_not_modified(self) -> None:
        server = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        client = CacheHeader(updated_at=datetime(2026, 1, 2, tzinfo=UTC))
        assert server.is_modified(client) is False

    def test_updated_at_after_client_is_modified(self) -> None:
        server = CacheHeader(updated_at=datetime(2026, 1, 2, tzinfo=UTC))
        client = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert server.is_modified(client) is True

    def test_updated_at_set_client_missing_is_modified(self) -> None:
        server = CacheHeader(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert server.is_modified(CacheHeader(updated_at=None)) is True

    def test_no_validators_always_modified(self) -> None:
        h = CacheHeader()
        assert h.is_modified(CacheHeader()) is True
        assert h.is_modified(CacheHeader(etag='"x"')) is True

    def test_etag_takes_precedence_over_updated_at(self) -> None:
        # When etag is set, updated_at is ignored even if it would say modified.
        h = CacheHeader(etag='"abc"', updated_at=datetime(2030, 1, 1, tzinfo=UTC))
        assert (
            h.is_modified(CacheHeader(etag='"abc"', updated_at=datetime(2000, 1, 1, tzinfo=UTC)))
            is False
        )

    def test_has_any(self) -> None:
        assert CacheHeader().has_any() is False
        assert CacheHeader(etag='"x"').has_any() is True
        assert CacheHeader(updated_at=datetime.now(UTC)).has_any() is True
        assert CacheHeader(expire_at=datetime.now(UTC)).has_any() is True


# ---------------------------------------------------------------------------
# ETagCacheStrategy
# ---------------------------------------------------------------------------


class TestETagCacheStrategy:
    def test_produces_quoted_etag_no_updated_at(self) -> None:
        h = ETagCacheStrategy().get_cache_header([Item(id=1, label="x")])
        assert h.etag is not None
        assert h.etag.startswith('"') and h.etag.endswith('"')
        assert h.updated_at is None
        assert h.expire_at is None

    def test_stable_across_same_input(self) -> None:
        items = [Item(id=1, label="x"), Item(id=2, label="y")]
        a = ETagCacheStrategy().get_cache_header(items)
        b = ETagCacheStrategy().get_cache_header(items)
        assert a.etag == b.etag

    def test_changes_on_different_input(self) -> None:
        a = ETagCacheStrategy().get_cache_header([Item(id=1, label="x")])
        b = ETagCacheStrategy().get_cache_header([Item(id=1, label="y")])
        assert a.etag != b.etag

    def test_order_matters(self) -> None:
        # Different list order yields a different hash (concatenation is ordered).
        a = ETagCacheStrategy().get_cache_header([Item(id=1, label="x"), Item(id=2, label="y")])
        b = ETagCacheStrategy().get_cache_header([Item(id=2, label="y"), Item(id=1, label="x")])
        assert a.etag != b.etag

    def test_expire_in_sets_expire_at(self) -> None:
        h = ETagCacheStrategy(expire_in=60).get_cache_header([Item(id=1, label="x")])
        assert h.expire_at is not None
        assert h.expire_at > datetime.now(UTC)

    def test_secret_redacted_value_is_stable(self) -> None:
        # Secrets redact to a fixed sentinel, so the hash is stable regardless
        # of the underlying secret value when redaction is in effect (no context).
        a = ETagCacheStrategy().get_cache_header([SecretItem(id=1, label="x", secret="**********")])
        b = ETagCacheStrategy().get_cache_header([SecretItem(id=1, label="x", secret="**********")])
        assert a.etag == b.etag

    def test_empty_list_produces_etag(self) -> None:
        h = ETagCacheStrategy().get_cache_header([])
        assert h.etag is not None


# ---------------------------------------------------------------------------
# LastModifiedCacheStrategy
# ---------------------------------------------------------------------------


class TestLastModifiedCacheStrategy:
    def test_max_updated_at(self) -> None:
        items = [
            Item(id=1, label="x", updated_at=datetime(2026, 1, 1, tzinfo=UTC)),
            Item(id=2, label="y", updated_at=datetime(2026, 1, 5, tzinfo=UTC)),
            Item(id=3, label="z", updated_at=datetime(2026, 1, 3, tzinfo=UTC)),
        ]
        h = LastModifiedCacheStrategy().get_cache_header(items)
        assert h.updated_at == datetime(2026, 1, 5, tzinfo=UTC)
        assert h.etag is None

    def test_missing_updated_at_does_not_advance_max(self) -> None:
        items = [
            Item(id=1, label="x", updated_at=datetime(2026, 1, 5, tzinfo=UTC)),
            Item(id=2, label="y", updated_at=None),
        ]
        h = LastModifiedCacheStrategy().get_cache_header(items)
        assert h.updated_at == datetime(2026, 1, 5, tzinfo=UTC)

    def test_all_missing_updated_at(self) -> None:
        h = LastModifiedCacheStrategy().get_cache_header([Item(id=1, label="x", updated_at=None)])
        # Falls back to datetime.min (no real last-modified, but a stable value).
        assert h.updated_at is not None

    def test_naive_updated_at_normalized(self) -> None:
        h = LastModifiedCacheStrategy().get_cache_header(
            [Item(id=1, label="x", updated_at=datetime(2025, 6, 1))]
        )
        assert h.updated_at is not None
        assert h.updated_at.tzinfo is not None

    def test_expire_in_sets_expire_at(self) -> None:
        h = LastModifiedCacheStrategy(expire_in=30).get_cache_header(
            [Item(id=1, label="x", updated_at=datetime(2026, 1, 1, tzinfo=UTC))]
        )
        assert h.expire_at is not None


# ---------------------------------------------------------------------------
# OptimisticCacheStrategy
# ---------------------------------------------------------------------------


class TestOptimisticCacheStrategy:
    def test_produces_only_expire_at(self) -> None:
        h = OptimisticCacheStrategy(expire_in=60).get_cache_header([Item(id=1, label="x")])
        assert h.expire_at is not None
        assert h.etag is None
        assert h.updated_at is None

    def test_requires_positive_expire_in(self) -> None:
        with pytest.raises(ValueError, match="expire_in > 0"):
            OptimisticCacheStrategy(expire_in=0)

    def test_negative_expire_in_rejected(self) -> None:
        # Negative hits the shared base validator (>= 0) before the Optimistic
        # subclass's own (> 0) check.
        with pytest.raises(ValueError, match=">= 0"):
            OptimisticCacheStrategy(expire_in=-5)


# ---------------------------------------------------------------------------
# expire_in validation on the base
# ---------------------------------------------------------------------------


class TestExpireInValidation:
    def test_negative_expire_in_rejected_on_etag(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            ETagCacheStrategy(expire_in=-1)

    def test_negative_expire_in_rejected_on_last_modified(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            LastModifiedCacheStrategy(expire_in=-1)

    def test_zero_expire_in_allowed(self) -> None:
        assert ETagCacheStrategy(expire_in=0).expire_in == 0
        assert LastModifiedCacheStrategy(expire_in=0).expire_in == 0


# ---------------------------------------------------------------------------
# Discriminated-union serialization
# ---------------------------------------------------------------------------


class TestCacheStrategySerialization:
    def test_roundtrip_etag(self) -> None:
        s = ETagCacheStrategy(expire_in=30)
        d = s.model_dump()
        assert d["kind"] == "ETagCacheStrategy"
        r = CacheStrategy.model_validate(d)
        assert isinstance(r, ETagCacheStrategy)
        assert r.expire_in == 30

    def test_roundtrip_last_modified(self) -> None:
        d = {"kind": "LastModifiedCacheStrategy", "expire_in": 10}
        r = CacheStrategy.model_validate(d)
        assert isinstance(r, LastModifiedCacheStrategy)
        assert r.expire_in == 10

    def test_roundtrip_optimistic(self) -> None:
        d = {"kind": "OptimisticCacheStrategy", "expire_in": 90}
        r = CacheStrategy.model_validate(d)
        assert isinstance(r, OptimisticCacheStrategy)
        assert r.expire_in == 90

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown kind"):
            CacheStrategy.model_validate({"kind": "Nope", "expire_in": 1})


# ---------------------------------------------------------------------------
# BaseResource.get_cache_strategy
# ---------------------------------------------------------------------------


class HasUpdated(SqlResource):
    id: int
    label: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NoUpdated(SqlResource):
    id: int
    label: str


class UpdatedUnreadable(SqlResource):
    id: int
    label: str
    updated_at: Annotated[
        datetime, Field(default_factory=lambda: datetime.now(UTC)), ResourceyField(readable=False)
    ]


class CustomStrategy(SqlResource):
    id: int
    label: str

    @classmethod
    def get_cache_strategy(cls) -> CacheStrategy[Any]:  # type: ignore[type-arg]
        return OptimisticCacheStrategy(expire_in=42)


class EtagExpiring(SqlResource):
    """ETag strategy with a freshness window (Cache-Control + Expires)."""

    id: int
    label: str

    @classmethod
    def get_cache_strategy(cls) -> CacheStrategy[Any]:  # type: ignore[type-arg]
        return ETagCacheStrategy(expire_in=120)


class OptimisticResource(SqlResource):
    """Optimistic strategy: freshness only, no validators."""

    id: int
    label: str

    @classmethod
    def get_cache_strategy(cls) -> CacheStrategy[Any]:  # type: ignore[type-arg]
        return OptimisticCacheStrategy(expire_in=60)


class NoCacheResource(SqlResource):
    """A resource whose strategy yields nothing (no headers emitted)."""

    id: int
    label: str

    @classmethod
    def get_cache_strategy(cls) -> CacheStrategy[Any]:  # type: ignore[type-arg]
        class _NoneStrategy(CacheStrategy[Any]):  # type: ignore[type-arg]
            def get_cache_header(
                self, models: list[Any], *, context: dict[str, Any] | None = None
            ) -> CacheHeader:
                return CacheHeader()

        return _NoneStrategy()


for _r in (
    HasUpdated,
    NoUpdated,
    UpdatedUnreadable,
    CustomStrategy,
    EtagExpiring,
    OptimisticResource,
    NoCacheResource,
):
    _r.get_sql_alchemy_model()


class TestGetCacheStrategy:
    def test_defaults_to_last_modified_when_updated_at_readable(self) -> None:
        assert isinstance(HasUpdated().get_cache_strategy(), LastModifiedCacheStrategy)

    def test_defaults_to_etag_when_no_updated_at(self) -> None:
        assert isinstance(NoUpdated().get_cache_strategy(), ETagCacheStrategy)

    def test_defaults_to_etag_when_updated_at_not_readable(self) -> None:
        assert isinstance(UpdatedUnreadable().get_cache_strategy(), ETagCacheStrategy)

    def test_cached_on_class(self) -> None:
        assert HasUpdated().get_cache_strategy() is HasUpdated().get_cache_strategy()

    def test_override_seam(self) -> None:
        s = CustomStrategy().get_cache_strategy()
        assert isinstance(s, OptimisticCacheStrategy)
        assert s.expire_in == 42


# ---------------------------------------------------------------------------
# ResourceService cache header computation
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(ResourceyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with session_factory() as sess:
        yield sess


class TestServiceComputeCacheHeader:
    def test_compute_cache_header_etag_for_no_updated_resource(self) -> None:
        svc = SqlService(NoUpdated(), session=None)
        header = svc.compute_cache_header([NoUpdated().get_read_model()(id=1, label="x")])
        assert header is not None
        assert header.etag is not None

    def test_compute_cache_header_last_modified_for_updated_resource(self) -> None:
        svc = SqlService(HasUpdated(), session=None)
        rm = HasUpdated().get_read_model()(
            id=1, label="x", updated_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        header = svc.compute_cache_header([rm])
        assert header is not None
        assert header.updated_at is not None

    def test_compute_count_cache_header_etag(self) -> None:
        svc = SqlService(NoUpdated(), session=None)
        h1 = svc.compute_count_cache_header(3, None)
        assert h1 is not None
        assert h1.etag is not None
        assert h1.updated_at is None

    def test_compute_count_cache_header_distinct_for_distinct_count(self) -> None:
        svc = SqlService(NoUpdated(), session=None)
        a = svc.compute_count_cache_header(3, None)
        b = svc.compute_count_cache_header(4, None)
        assert a is not None and b is not None
        assert a.etag != b.etag

    def test_compute_count_cache_header_always_returns_etag(self) -> None:
        # Count is count-derived (independent of the strategy's get_cache_header),
        # so it always produces an ETag even when the strategy yields nothing.
        svc = SqlService(NoCacheResource(), session=None)
        # compute_cache_header yields None (strategy produces nothing)...
        assert (
            svc.compute_cache_header([NoCacheResource().get_read_model()(id=1, label="x")]) is None
        )
        # ...but count is count-derived, so it still has an ETag.
        h = svc.compute_count_cache_header(5, None)
        assert h is not None
        assert h.etag is not None


# ---------------------------------------------------------------------------
# HTTP-layer integration
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_factory(session_factory: async_sessionmaker[AsyncSession]):
    def _build(resource: type[SqlResource]) -> AsyncClient:
        instance = resource()
        instance.on_register()
        instance._session_factory = session_factory
        app = FastAPI()
        register_routes(app, instance)
        register_error_handlers(app)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return _build


class TestHttpCacheHeaders:
    @pytest.mark.asyncio
    async def test_read_emits_etag(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            created = await client.post("/no-updateds", json={"label": "x"})
            etag = created.headers.get("etag")
            assert etag is not None
            rid = created.json()["id"]
            r = await client.get(f"/no-updateds/{rid}")
            assert r.status_code == 200
            assert r.headers["etag"] == etag

    @pytest.mark.asyncio
    async def test_read_emits_last_modified(self, client_factory, session_factory) -> None:
        async with client_factory(HasUpdated) as client:
            created = await client.post("/has-updateds", json={"label": "x"})
            lm = created.headers.get("last-modified")
            assert lm is not None
            rid = created.json()["id"]
            r = await client.get(f"/has-updateds/{rid}")
            assert r.status_code == 200
            assert "last-modified" in r.headers

    @pytest.mark.asyncio
    async def test_read_304_on_matching_etag(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            created = await client.post("/no-updateds", json={"label": "x"})
            etag = created.headers["etag"]
            rid = created.json()["id"]
            r = await client.get(f"/no-updateds/{rid}", headers={"If-None-Match": etag})
            assert r.status_code == 304
            assert r.content == b""
            assert r.headers["etag"] == etag

    @pytest.mark.asyncio
    async def test_read_200_on_mismatched_etag(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            created = await client.post("/no-updateds", json={"label": "x"})
            rid = created.json()["id"]
            r = await client.get(f"/no-updateds/{rid}", headers={"If-None-Match": '"deadbeef"'})
            assert r.status_code == 200
            assert r.json()["id"] == rid

    @pytest.mark.asyncio
    async def test_read_304_on_if_modified_since(self, client_factory, session_factory) -> None:
        async with client_factory(HasUpdated) as client:
            created = await client.post("/has-updateds", json={"label": "x"})
            lm = created.headers["last-modified"]
            rid = created.json()["id"]
            # Send the same Last-Modified → server's updated_at <= client's → 304.
            r = await client.get(f"/has-updateds/{rid}", headers={"If-Modified-Since": lm})
            assert r.status_code == 304
            assert "last-modified" in r.headers

    @pytest.mark.asyncio
    async def test_search_emits_etag_and_304(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            await client.post("/no-updateds", json={"label": "a"})
            r1 = await client.get("/no-updateds")
            assert r1.status_code == 200
            etag = r1.headers["etag"]
            r2 = await client.get("/no-updateds", headers={"If-None-Match": etag})
            assert r2.status_code == 304

    @pytest.mark.asyncio
    async def test_count_emits_etag_and_304(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            await client.post("/no-updateds", json={"label": "a"})
            r1 = await client.get("/no-updateds/count")
            assert r1.status_code == 200
            etag = r1.headers["etag"]
            r2 = await client.get("/no-updateds/count", headers={"If-None-Match": etag})
            assert r2.status_code == 304

    @pytest.mark.asyncio
    async def test_count_etag_changes_with_count(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            r1 = await client.get("/no-updateds/count")
            await client.post("/no-updateds", json={"label": "a"})
            r2 = await client.get("/no-updateds/count")
            assert r1.headers["etag"] != r2.headers["etag"]

    @pytest.mark.asyncio
    async def test_batch_read_emits_etag_and_304(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            a = (await client.post("/no-updateds", json={"label": "a"})).json()["id"]
            r1 = await client.get(f"/no-updateds/batch-read?id={a}")
            assert r1.status_code == 200
            etag = r1.headers["etag"]
            r2 = await client.get(
                f"/no-updateds/batch-read?id={a}", headers={"If-None-Match": etag}
            )
            assert r2.status_code == 304

    @pytest.mark.asyncio
    async def test_batch_edit_emits_etag(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            a = (await client.post("/no-updateds", json={"label": "a"})).json()["id"]
            r = await client.post("/no-updateds/batch-edit", json=[{"id": a, "label": "b"}])
            assert r.status_code == 200
            assert "etag" in r.headers

    @pytest.mark.asyncio
    async def test_create_emits_etag(self, client_factory, session_factory) -> None:
        async with client_factory(NoUpdated) as client:
            r = await client.post("/no-updateds", json={"label": "a"})
            assert r.status_code == 201
            assert "etag" in r.headers

    @pytest.mark.asyncio
    async def test_cache_control_emitted_with_expire_in(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(EtagExpiring) as client:
            created = await client.post("/etag-expirings", json={"label": "x"})
            rid = created.json()["id"]
            r = await client.get(f"/etag-expirings/{rid}")
            assert r.status_code == 200
            assert r.headers["cache-control"].startswith("max-age=")
            assert "expires" in r.headers

    @pytest.mark.asyncio
    async def test_optimistic_emits_cache_control_only(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(OptimisticResource) as client:
            created = await client.post("/optimistic-resources", json={"label": "x"})
            rid = created.json()["id"]
            r = await client.get(f"/optimistic-resources/{rid}")
            assert r.status_code == 200
            # Optimistic emits only freshness, no validators.
            assert "etag" not in r.headers
            assert "last-modified" not in r.headers
            assert r.headers["cache-control"].startswith("max-age=")

    @pytest.mark.asyncio
    async def test_304_response_includes_cache_control(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(EtagExpiring) as client:
            created = await client.post("/etag-expirings", json={"label": "x"})
            etag = created.headers["etag"]
            rid = created.json()["id"]
            r = await client.get(f"/etag-expirings/{rid}", headers={"If-None-Match": etag})
            assert r.status_code == 304
            assert r.headers["etag"] == etag
            assert r.headers["cache-control"].startswith("max-age=")

    @pytest.mark.asyncio
    async def test_invalid_if_modified_since_treated_as_modified(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(HasUpdated) as client:
            created = await client.post("/has-updateds", json={"label": "x"})
            rid = created.json()["id"]
            r = await client.get(
                f"/has-updateds/{rid}", headers={"If-Modified-Since": "not-a-date"}
            )
            # Unparseable header → no client validator → body sent.
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Fix #1 - ETag reflects the serialization context (tracks the response body)
# ---------------------------------------------------------------------------


class _ContextSensitiveItem(BaseModel):
    """A model whose serialized form depends on the pydantic context.

    Mirrors how secret-bearing fields behave: the same underlying value
    serializes differently with vs. without (or with distinct) contexts. The
    ETag must follow that so it validates the bytes actually sent.
    """

    id: int
    secret: str = "redacted"

    @field_serializer("secret")
    def _serialize_secret(self, _value: str, info: Any) -> str:
        ctx = getattr(info, "context", None) or {}
        token = ctx.get("token")
        if token is not None:
            return f"enc:{token}"
        if ctx.get("expose"):
            return "plaintext"
        return "redacted"


class TestETagSerializationContext:
    def test_etag_changes_with_context(self) -> None:
        """A context that changes the serialized form must change the ETag."""
        items = [_ContextSensitiveItem(id=1)]
        redacted = ETagCacheStrategy().get_cache_header(items)
        exposed = ETagCacheStrategy().get_cache_header(items, context={"expose": True})
        encrypted = ETagCacheStrategy().get_cache_header(items, context={"token": "abc"})
        assert redacted.etag != exposed.etag
        assert redacted.etag != encrypted.etag
        assert exposed.etag != encrypted.etag

    def test_etag_tracks_distinct_context_values(self) -> None:
        """Distinct encryption tokens (e.g. non-deterministic IVs) yield distinct ETags."""
        items = [_ContextSensitiveItem(id=1)]
        a = ETagCacheStrategy().get_cache_header(items, context={"token": "iv-1"})
        b = ETagCacheStrategy().get_cache_header(items, context={"token": "iv-2"})
        assert a.etag != b.etag

    def test_etag_stable_for_same_context(self) -> None:
        items = [_ContextSensitiveItem(id=1)]
        a = ETagCacheStrategy().get_cache_header(items, context={"token": "same"})
        b = ETagCacheStrategy().get_cache_header(items, context={"token": "same"})
        assert a.etag == b.etag

    def test_default_context_redacts_like_no_context(self) -> None:
        items = [_ContextSensitiveItem(id=1)]
        none_ctx = ETagCacheStrategy().get_cache_header(items, context=None)
        default = ETagCacheStrategy().get_cache_header(items)
        assert none_ctx.etag == default.etag

    def test_service_threads_context_into_etag(self, session_factory) -> None:
        """ResourceService.compute_cache_header passes its serialization context."""

        class CtxResource(SqlResource):
            id: int
            secret: str = "redacted"

            @classmethod
            def get_cache_strategy(cls):  # type: ignore[override]
                return ETagCacheStrategy()

        CtxResource().get_sql_alchemy_model()
        rm_cls = type("CtxRead", (_ContextSensitiveItem,), {})
        svc_no_ctx = SqlService(CtxResource(), session=None)
        svc_with_ctx = SqlService(
            CtxResource(),
            session=None,
            serialization_context={"expose": True},
        )
        item = rm_cls(id=1)
        h_none = svc_no_ctx.compute_cache_header([item])
        h_exposed = svc_with_ctx.compute_cache_header([item])
        assert h_none is not None and h_exposed is not None
        assert h_none.etag != h_exposed.etag


# ---------------------------------------------------------------------------
# Fix #2 - 304 short-circuit is guarded to safe methods (GET/HEAD)
# ---------------------------------------------------------------------------


class TestSafeMethodGuard:
    @pytest.mark.asyncio
    async def test_get_with_if_none_match_star_returns_304(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            await client.post("/no-updateds", json={"label": "x"})
            r = await client.get("/no-updateds", headers={"If-None-Match": "*"})
            assert r.status_code == 304

    @pytest.mark.asyncio
    async def test_create_does_not_304_with_if_none_match_star(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            r = await client.post(
                "/no-updateds", json={"label": "x"}, headers={"If-None-Match": "*"}
            )
            assert r.status_code == 201
            assert r.content != b""

    @pytest.mark.asyncio
    async def test_update_does_not_304_with_if_none_match_star(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            created = await client.post("/no-updateds", json={"label": "x"})
            rid = created.json()["id"]
            r = await client.patch(
                f"/no-updateds/{rid}",
                json={"label": "y"},
                headers={"If-None-Match": "*"},
            )
            assert r.status_code == 200
            assert r.content != b""

    @pytest.mark.asyncio
    async def test_batch_edit_does_not_304_with_if_none_match_star(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            a = (await client.post("/no-updateds", json={"label": "a"})).json()["id"]
            r = await client.post(
                "/no-updateds/batch-edit",
                json=[{"id": a, "label": "b"}],
                headers={"If-None-Match": "*"},
            )
            assert r.status_code == 200
            assert r.content != b""

    @pytest.mark.asyncio
    async def test_mutation_routes_still_emit_etag(self, client_factory, session_factory) -> None:
        """Unsafe methods emit ETag/Cache-Control but never short-circuit to 304."""
        async with client_factory(NoUpdated) as client:
            created = await client.post("/no-updateds", json={"label": "x"})
            assert "etag" in created.headers
            rid = created.json()["id"]
            etag = created.headers["etag"]
            r = await client.patch(
                f"/no-updateds/{rid}",
                json={"label": "y"},
                headers={"If-None-Match": etag},
            )
            assert r.status_code == 200
            assert r.content != b""


# ---------------------------------------------------------------------------
# Fix #4 - If-None-Match list / "*" handling
# ---------------------------------------------------------------------------


class TestIfNoneMatchListAndStar:
    def test_star_matches_any_etag(self) -> None:
        server = CacheHeader(etag='"abc"')
        assert server.is_modified(CacheHeader(etag="*")) is False

    def test_list_with_matching_etag_is_not_modified(self) -> None:
        server = CacheHeader(etag='"abc"')
        client = CacheHeader(etag='"abc", "def"')
        assert server.is_modified(client) is False

    def test_list_without_matching_etag_is_modified(self) -> None:
        server = CacheHeader(etag='"abc"')
        client = CacheHeader(etag='"def", "ghi"')
        assert server.is_modified(client) is True

    def test_list_with_weak_etag_then_second_token_matches(self) -> None:
        server = CacheHeader(etag='"abc"')
        # The weak token does not match the strong server etag, but the second
        # (strong) token does.
        client = CacheHeader(etag='W/"xyz", "abc"')
        assert server.is_modified(client) is False

    def test_star_is_not_modified_even_when_server_etag_differs(self) -> None:
        server = CacheHeader(etag='"anything-at-all"')
        assert server.is_modified(CacheHeader(etag="*")) is False

    @pytest.mark.asyncio
    async def test_get_with_matching_list_etag_returns_304(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            await client.post("/no-updateds", json={"label": "x"})
            r1 = await client.get("/no-updateds")
            etag = r1.headers["etag"]
            r2 = await client.get("/no-updateds", headers={"If-None-Match": f'"deadbeef", {etag}'})
            assert r2.status_code == 304

    @pytest.mark.asyncio
    async def test_get_with_non_matching_list_returns_200(
        self, client_factory, session_factory
    ) -> None:
        async with client_factory(NoUpdated) as client:
            await client.post("/no-updateds", json={"label": "x"})
            r = await client.get("/no-updateds", headers={"If-None-Match": '"one", "two", "three"'})
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Fix #3 - canonical JSON is stable for reordered nested keys
# ---------------------------------------------------------------------------


class _NestedItem(BaseModel):
    id: int
    meta: dict[str, str] = {}


class TestStableCanonicalJson:
    def test_etag_stable_for_reordered_nested_keys(self) -> None:
        """Nested dicts with the same content but different key order hash equally."""
        a = ETagCacheStrategy().get_cache_header([_NestedItem(id=1, meta={"b": "2", "a": "1"})])
        b = ETagCacheStrategy().get_cache_header([_NestedItem(id=1, meta={"a": "1", "b": "2"})])
        assert a.etag == b.etag

    def test_etag_changes_when_nested_content_differs(self) -> None:
        a = ETagCacheStrategy().get_cache_header([_NestedItem(id=1, meta={"a": "1"})])
        b = ETagCacheStrategy().get_cache_header([_NestedItem(id=1, meta={"a": "2"})])
        assert a.etag != b.etag
