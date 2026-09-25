"""Tests for ``v2`` sorting (issue #97).

Split by layer, mirroring ``test_v2_filtering.py``:

* ``TestCoreSortOrder`` — the storage-agnostic ``compare`` semantics and the
  ``AttrSortOrder`` node in ``v2/util/sort_order.py``.
* ``TestSqlSortConversion`` — the ``v2/sql/sort_converter.py`` translation,
  including the identifier tie-breaker and the registry completeness assert.
* ``TestSortSurface`` — the derived sortable surface, the service-level
  validation and cursor/sort binding, and the ordering visible over HTTP.
* ``TestSortCache`` — distinct sorts produce distinct validators.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.dto import DtoField
from resourcey.v2.core.errors import InvalidInputError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.service import SearchSpec
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.http.app import create_app
from resourcey.v2.sql import cursor as cursor_module
from resourcey.v2.sql.resource import SqlResource
from resourcey.v2.sql.sort_converter import (
    _REGISTRY,
    SqlSortContext,
    SqlSortConverter,
    register_sort_order,
)
from resourcey.v2.util.sort_order import AttrSortOrder, CompareResult, SortOrder


class Row:
    """A tiny object for exercising ``compare`` without a backend."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


# ---------------------------------------------------------------------------
# Core: compare semantics
# ---------------------------------------------------------------------------


class TestCoreSortOrder:
    def test_ascending_compare(self) -> None:
        order = AttrSortOrder(attribute="score")
        assert order.compare(Row(score=1), Row(score=2)) is CompareResult.LESS
        assert order.compare(Row(score=2), Row(score=1)) is CompareResult.GREATER
        assert order.compare(Row(score=1), Row(score=1)) is CompareResult.SAME

    def test_descending_is_the_mirror(self) -> None:
        ascending = AttrSortOrder(attribute="score")
        descending = AttrSortOrder(attribute="score", descending=True)
        for left, right in ((1, 2), (2, 1), (1, 1)):
            forward = ascending.compare(Row(score=left), Row(score=right))
            backward = descending.compare(Row(score=left), Row(score=right))
            if forward is CompareResult.SAME:
                assert backward is CompareResult.SAME
            else:
                assert backward is not forward

    def test_reads_objects_and_mappings(self) -> None:
        order = AttrSortOrder(attribute="score")
        assert order.compare(Row(score=1), Row(score=2)) is CompareResult.LESS
        assert order.compare({"score": 1}, {"score": 2}) is CompareResult.LESS
        # A missing attribute sorts first (None).
        assert order.compare(Row(other=1), Row(score=1)) is CompareResult.LESS

    def test_none_sorts_first(self) -> None:
        order = AttrSortOrder(attribute="score")
        assert order.compare(Row(score=None), Row(score=1)) is CompareResult.LESS
        assert order.compare(Row(score=1), Row(score=None)) is CompareResult.GREATER
        assert order.compare(Row(score=None), Row(score=None)) is CompareResult.SAME

    def test_frozen_and_hashable(self) -> None:
        order = AttrSortOrder(attribute="score")
        with pytest.raises(ValidationError):
            order.attribute = "other"  # type: ignore[misc]
        assert {order, AttrSortOrder(attribute="score")} == {order}

    def test_dump_and_roundtrip(self) -> None:
        order = AttrSortOrder(attribute="score", descending=True)
        restored = SortOrder.model_validate(order.model_dump())
        assert restored == order
        assert restored.compare(Row(score=1), Row(score=2)) is CompareResult.GREATER

    def test_descending_defaults_to_false(self) -> None:
        assert AttrSortOrder(attribute="score").descending is False

    def test_reverse_leaves_same_alone(self) -> None:
        from resourcey.v2.util.sort_order import _reverse

        assert _reverse(CompareResult.SAME) is CompareResult.SAME
        assert _reverse(CompareResult.LESS) is CompareResult.GREATER
        assert _reverse(CompareResult.GREATER) is CompareResult.LESS


# ---------------------------------------------------------------------------
# SQL conversion
# ---------------------------------------------------------------------------


class SortBase(DeclarativeBase):
    pass


class Thing(SortBase):
    __tablename__ = "sort_things"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))
    rank: Mapped[int] = mapped_column(Integer)


class TestSqlSortConversion:
    def _context(self) -> SqlSortContext:
        return SqlSortContext(
            columns={"name": Thing.__table__.c.name, "rank": Thing.__table__.c.rank},
            id_column=Thing.__table__.c.id,
        )

    def test_apply_appends_the_identifier_tie_breaker(self) -> None:
        stmt = select(Thing.__table__)
        ordered = SqlSortConverter(self._context()).apply(stmt, AttrSortOrder(attribute="rank"))
        compiled = str(ordered.compile())
        # NULLs first ascending, so the SQL order matches AttrSortOrder.compare.
        assert "ORDER BY sort_things.rank ASC NULLS FIRST, sort_things.id ASC" in compiled

    def test_apply_mirrors_for_descending(self) -> None:
        stmt = select(Thing.__table__)
        ordered = SqlSortConverter(self._context()).apply(
            stmt, AttrSortOrder(attribute="rank", descending=True)
        )
        # Only the sort column mirrors; the id tie-breaker stays ascending, and
        # NULLs move last so the null block reverses with the rest.
        assert "ORDER BY sort_things.rank DESC NULLS LAST, sort_things.id ASC" in str(
            ordered.compile()
        )

    def test_unknown_attribute_raises(self) -> None:
        with pytest.raises(InvalidInputError, match="Unknown or non-sortable"):
            SqlSortConverter(self._context()).apply(
                select(Thing.__table__), AttrSortOrder(attribute="nope")
            )

    def test_registry_is_enumerable(self) -> None:
        assert AttrSortOrder in _REGISTRY

    def test_unregistered_node_raises(self) -> None:
        class Unknown(SortOrder[Any]):
            def compare(self, a: Any, b: Any) -> CompareResult:
                return CompareResult.SAME

        with pytest.raises(InvalidInputError, match="No SQL conversion"):
            SqlSortConverter(self._context()).apply(select(Thing.__table__), Unknown())

    def test_register_sort_order_is_extensible(self) -> None:
        class Custom(SortOrder[Any]):
            attribute: str = "name"

            def compare(self, a: Any, b: Any) -> CompareResult:
                return CompareResult.SAME

        def _handler(ctx: SqlSortContext, node: SortOrder[Any]) -> Any:
            return ctx.id_column.asc()

        register_sort_order(Custom, _handler)
        try:
            assert Custom in _REGISTRY
        finally:
            del _REGISTRY[Custom]

    def test_completeness_assert_fires_when_a_handler_is_missing(self) -> None:
        import resourcey.v2.sql.sort_converter as sc

        saved = sc._REGISTRY.pop(AttrSortOrder)
        try:
            with pytest.raises(RuntimeError, match="registry is incomplete"):
                sc._assert_registry_is_complete()
        finally:
            sc._REGISTRY[AttrSortOrder] = saved


# ---------------------------------------------------------------------------
# Derived surface + service + HTTP
# ---------------------------------------------------------------------------


class ApiBase(DeclarativeBase):
    pass


class Widget(ApiBase):
    __tablename__ = "sort_widgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))
    rank: Mapped[int] = mapped_column(Integer)
    secret: Mapped[str] = mapped_column(
        String(100),
        info={"dto_field": DtoField(in_read_response=False, in_search_response=False)},
    )


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-sorting")
        )
    )


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[tuple[AsyncClient, SqlResource[Any]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    widgets = SqlResource(
        Widget,
        session_factory=maker,
        path="sort-widgets",
        encryption_service=_encryption(),
    )
    async with engine.begin() as conn:
        await conn.run_sync(ApiBase.metadata.create_all)
    manifest = Manifest(resources=[widgets])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for title, rank in (("alpha", 2), ("beta", 1), ("gamma", 2)):
                resp = await client.post(
                    "/sort-widgets", json={"title": title, "rank": rank, "secret": "s"}
                )
                assert resp.status_code == 201
            yield client, widgets
    await engine.dispose()


async def _titles(resp: Any) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["items"]]


class TestSortSurface:
    async def test_default_ordering_is_unchanged(self, api_client) -> None:
        client, _ = api_client
        assert await _titles(await client.get("/sort-widgets")) == ["alpha", "beta", "gamma"]

    async def test_sort_ascending_and_descending(self, api_client) -> None:
        client, _ = api_client
        assert await _titles(await client.get("/sort-widgets", params={"sort": "title"})) == [
            "alpha",
            "beta",
            "gamma",
        ]
        assert await _titles(
            await client.get("/sort-widgets", params={"sort": "title", "desc": "true"})
        ) == ["gamma", "beta", "alpha"]

    async def test_ties_break_on_the_identifier(self, api_client) -> None:
        client, _ = api_client
        # alpha (id 1) and gamma (id 3) both rank 2; the id keeps the order stable.
        assert await _titles(await client.get("/sort-widgets", params={"sort": "rank"})) == [
            "beta",
            "alpha",
            "gamma",
        ]
        # Descending mirrors the sort key but the identifier tie-breaker stays
        # ascending, so rank 2 rows (alpha id 1, gamma id 3) precede rank 1.
        assert await _titles(
            await client.get("/sort-widgets", params={"sort": "rank", "desc": "true"})
        ) == ["alpha", "gamma", "beta"]

    async def test_sortable_surface_matches_the_read_model(self, api_client) -> None:
        _client, widgets = api_client
        assert "title" in widgets.get_sortable_fields()
        assert "secret" not in widgets.get_sortable_fields()
        assert widgets.get_sort_order_type() is None

    async def test_unknown_sort_field_is_400(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/sort-widgets", params={"sort": "nope"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_input"

    async def test_hidden_field_is_not_sortable(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/sort-widgets", params={"sort": "secret"})
        assert resp.status_code == 400

    async def test_sort_appears_in_openapi(self, api_client) -> None:
        client, _ = api_client
        spec = (await client.get("/openapi.json")).json()
        params = {p["name"] for p in spec["paths"]["/sort-widgets"]["get"]["parameters"]}
        assert {"sort", "desc"} <= params

    async def test_paging_follows_the_sort_order(self, api_client) -> None:
        client, _ = api_client
        first = await client.get("/sort-widgets", params={"sort": "rank", "limit": 1})
        assert await _titles(first) == ["beta"]
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        second = await client.get(
            "/sort-widgets", params={"sort": "rank", "limit": 2, "cursor": cursor}
        )
        assert await _titles(second) == ["alpha", "gamma"]
        assert second.json()["next_cursor"] is None

    async def test_cursor_reused_under_a_different_sort_is_400(self, api_client) -> None:
        client, _ = api_client
        first = await client.get("/sort-widgets", params={"sort": "rank", "limit": 1})
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        mismatch = await client.get("/sort-widgets", params={"sort": "title", "cursor": cursor})
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == "invalid_input"
        # Reusing a sort cursor with no sort at all is rejected too.
        no_sort = await client.get("/sort-widgets", params={"cursor": cursor})
        assert no_sort.status_code == 400
        # Reusing it under the same sort succeeds.
        same = await client.get("/sort-widgets", params={"sort": "rank", "cursor": cursor})
        assert same.status_code == 200

    async def test_direction_change_with_a_cursor_is_400(self, api_client) -> None:
        client, _ = api_client
        first = await client.get(
            "/sort-widgets", params={"sort": "rank", "desc": "true", "limit": 1}
        )
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        flipped = await client.get(
            "/sort-widgets", params={"sort": "rank", "desc": "false", "cursor": cursor}
        )
        assert flipped.status_code == 400

    async def test_descending_paging_visits_every_row(self, api_client) -> None:
        # Regression: descending keyset paging mirrored the id tie-breaker too,
        # so walking pages under `desc` silently skipped rows.
        client, _ = api_client
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"sort": "rank", "desc": "true", "limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            resp = await client.get("/sort-widgets", params=params)
            seen.extend(await _titles(resp))
            cursor = resp.json()["next_cursor"]
            if cursor is None:
                break
        # The full descending order, with no row lost and none repeated.
        assert seen == ["alpha", "gamma", "beta"]

    async def test_descending_ties_step_to_a_greater_id(self, api_client) -> None:
        client, _ = api_client
        # alpha (id 1) and gamma (id 3) share rank 2; the cursor at alpha must
        # seek to gamma, not back to rank 1.
        first = await client.get(
            "/sort-widgets", params={"sort": "rank", "desc": "true", "limit": 1}
        )
        assert await _titles(first) == ["alpha"]
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        second = await client.get(
            "/sort-widgets", params={"sort": "rank", "desc": "true", "cursor": cursor}
        )
        assert await _titles(second) == ["gamma", "beta"]

    async def test_desc_without_sort_pages_normally(self, api_client) -> None:
        # Regression: `desc` with no `sort` left the page ordered by id ascending
        # but encoded a descending cursor, which the next request rejected.
        client, _ = api_client
        first = await client.get("/sort-widgets", params={"desc": "true", "limit": 1})
        assert await _titles(first) == ["alpha"]
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        second = await client.get("/sort-widgets", params={"desc": "true", "cursor": cursor})
        assert await _titles(second) == ["beta", "gamma"]

    async def test_garbage_cursor_is_400_not_500(self, api_client) -> None:
        # Regression: a malformed cursor raised a bare ValueError and surfaced 500.
        client, _ = api_client
        resp = await client.get("/sort-widgets", params={"cursor": "not-a-cursor"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_input"

    async def test_service_rejects_a_non_sortable_field(self, api_client) -> None:
        _client, widgets = api_client
        with pytest.raises(InvalidInputError, match="sort field"):
            widgets.resolve_sort_order("secret", False)

    async def test_declared_sort_order_type_is_honoured(self) -> None:
        class Declared(SqlResource[Any]):
            def get_sort_order_type(self) -> type[SortOrder[Any]]:
                return AttrSortOrder

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        resource = Declared(Widget, session_factory=maker, encryption_service=_encryption())
        async with engine.begin() as conn:
            await conn.run_sync(ApiBase.metadata.create_all)
        async with resource.get_service() as service:
            await service.create(resource.get_dto_type()(title="b", rank=1, secret="s"))
            await service.create(resource.get_dto_type()(title="a", rank=2, secret="s"))
            page = await service.search(
                spec=SearchSpec(sort_order=resource.resolve_sort_order("title", False))
            )
            assert [item.title for item in page.items] == ["a", "b"]
            with pytest.raises(InvalidInputError, match="sort field"):
                resource.resolve_sort_order("nope", False)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class Renamed(ApiBase):
    """A model whose column names differ from the DTO attribute names."""

    __tablename__ = "sort_renamed"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column("display_label", String(50))
    rank: Mapped[int] = mapped_column("rank_value", Integer)


class TestSortColumnNameMapping:
    async def test_paging_a_renamed_column_sorts_and_cursors_correctly(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        resource = SqlResource(
            Renamed,
            session_factory=maker,
            path="renamed",
            encryption_service=_encryption(),
        )
        async with engine.begin() as conn:
            await conn.run_sync(ApiBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with resource.get_service() as service:
            # Two rows share rank 1, so paging must use the id tie-breaker and
            # the cursor's sort key must come from the mapped column, not the
            # DTO attribute name.
            for label, rank in (("a", 2), ("b", 1), ("c", 1)):
                await service.create(dto(label=label, rank=rank))
            first = await service.search(
                spec=SearchSpec(limit=1, sort_order=resource.resolve_sort_order("rank", False))
            )
            assert [item.label for item in first.items] == ["b"]
            assert first.next_cursor is not None
            second = await service.search(
                spec=SearchSpec(
                    limit=2,
                    cursor=first.next_cursor,
                    sort_order=resource.resolve_sort_order("rank", False),
                )
            )
            assert [item.label for item in second.items] == ["c", "a"]
            assert second.next_cursor is None
        await engine.dispose()


class TestSortCache:
    async def test_distinct_sorts_get_distinct_validators(self, api_client) -> None:
        client, _ = api_client
        ascending = await client.get("/sort-widgets", params={"sort": "title"})
        descending = await client.get("/sort-widgets", params={"sort": "title", "desc": "true"})
        assert ascending.headers["etag"] != descending.headers["etag"]

    async def test_same_sort_is_a_stable_validator(self, api_client) -> None:
        client, _ = api_client
        first = await client.get("/sort-widgets", params={"sort": "rank"})
        second = await client.get("/sort-widgets", params={"sort": "rank"})
        assert first.headers["etag"] == second.headers["etag"]

    async def test_conditional_request_with_the_sorted_etag_is_304(self, api_client) -> None:
        client, _ = api_client
        first = await client.get("/sort-widgets", params={"sort": "title", "desc": "true"})
        cached = await client.get(
            "/sort-widgets",
            params={"sort": "title", "desc": "true"},
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert cached.status_code == 304


class Nullable(ApiBase):
    """A model with a nullable sort column, for the NULL-key paging regression."""

    __tablename__ = "sort_nullables"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(50))
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TestNullableSortKeys:
    async def test_paging_a_nullable_column_visits_every_row(self) -> None:
        # Regression: a None sort key was serialized as the *string* "None", so
        # the next page compared the column against "None" and returned nothing.
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        resource = SqlResource(
            Nullable,
            session_factory=maker,
            path="nullables",
            encryption_service=_encryption(),
        )
        async with engine.begin() as conn:
            await conn.run_sync(ApiBase.metadata.create_all)
        dto = resource.get_dto_type()
        async with resource.get_service() as service:
            for label, score in (("a", None), ("b", 2), ("c", 1), ("d", None)):
                await service.create(dto(label=label, score=score))
            for descending in (False, True):
                order = resource.resolve_sort_order("score", descending)
                seen: list[str] = []
                cursor = None
                while True:
                    page = await service.search(
                        spec=SearchSpec(limit=1, cursor=cursor, sort_order=order)
                    )
                    seen.extend(item.label for item in page.items)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                # NULLs first ascending, last descending; ids break the ties.
                expected = ["a", "d", "c", "b"] if not descending else ["b", "c", "a", "d"]
                assert seen == expected
        await engine.dispose()

    async def test_null_cursor_key_round_trips(self) -> None:
        service = _encryption()
        cursor = cursor_module.encode_cursor(
            service, sort_field="score", ascending=True, sort_key=None, id_value=1
        )
        _field, _asc, sort_key, _id = cursor_module.decode_cursor(service, cursor)
        assert sort_key is None
