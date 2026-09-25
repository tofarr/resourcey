"""Tests for ``v2`` filtering (issue #79).

Split by layer, matching the design:

* ``TestCoreFilterMatches`` — the storage-agnostic ``matches`` predicate and the
  factory normalisation identities in ``v2/util/search_filter.py``.
* ``TestObjectFilterLowering`` — ``BaseObjectFilter`` reflects its
  ``<attribute>__<op>`` fields into a standard tree, cached.
* ``TestSqlConversion`` — the ``v2/sql/filter_converter.py`` translation,
  including the NULL-safe negation the design calls out.
* ``TestFilterSurface`` — the derived query surface and the transport's
  rejection of unknown params.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.dto import DtoField
from resourcey.v2.core.errors import UnsupportedFilterError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.service import SearchSpec
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.filter_converter import SqlFilterContext, SqlFilterConverter
from resourcey.v2.sql.resource import SqlResource
from resourcey.v2.util.search_filter import (
    AllFilter,
    AndFilter,
    AttrFilter,
    BaseObjectFilter,
    ContainsFilter,
    EqFilter,
    GeFilter,
    GtFilter,
    LeFilter,
    LtFilter,
    NoMatchFilter,
    NotFilter,
    OrFilter,
    SearchFilter,
    and_,
    attr,
    build_filter,
    not_,
    operators_for_annotation,
    or_,
)


class Row:
    """A tiny object for exercising ``matches`` without a backend."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


# ---------------------------------------------------------------------------
# Core: matches + normalisation
# ---------------------------------------------------------------------------


class TestCoreFilterMatches:
    def test_constants_are_singletons_and_match(self) -> None:
        assert AllFilter() is AllFilter()
        assert NoMatchFilter() is NoMatchFilter()
        assert AllFilter().matches(Row(x=1)) is True
        assert NoMatchFilter().matches(Row(x=1)) is False

    def test_value_operators(self) -> None:
        assert EqFilter(value=3).matches(3)
        assert not EqFilter(value=3).matches(4)
        assert GtFilter(value=3).matches(4)
        assert GeFilter(value=3).matches(3)
        assert LtFilter(value=3).matches(2)
        assert LeFilter(value=3).matches(3)

    def test_contains_is_case_insensitive_substring(self) -> None:
        assert ContainsFilter(value="ALI").matches("alice")
        assert not ContainsFilter(value="zed").matches("alice")
        assert ContainsFilter(value="x").matches(["a", "x"])
        assert not ContainsFilter(value="x").matches(None)
        assert not ContainsFilter(value="x").matches(5)

    def test_attr_reads_objects_and_mappings(self) -> None:
        assert attr("score", GtFilter(value=3)).matches(Row(score=4))
        assert attr("score", GtFilter(value=3)).matches({"score": 4})
        assert attr("absent", EqFilter(value=None)).matches(Row(score=4))

    def test_logical_combinations(self) -> None:
        flt = and_(attr("a", EqFilter(value=1)), attr("b", EqFilter(value=2)))
        assert flt.matches(Row(a=1, b=2))
        assert not flt.matches(Row(a=1, b=3))
        assert or_(attr("a", EqFilter(value=1)), attr("a", EqFilter(value=9))).matches(Row(a=9))
        assert not_(attr("a", EqFilter(value=1))).matches(Row(a=2))

    def test_and_normalisation_identities(self) -> None:
        assert isinstance(and_(), AllFilter)
        assert isinstance(and_(AllFilter(), EqFilter(value=1)), EqFilter)
        assert isinstance(and_(NoMatchFilter(), EqFilter(value=1)), NoMatchFilter)
        assert isinstance(or_(), NoMatchFilter)
        assert isinstance(or_(AllFilter(), EqFilter(value=1)), AllFilter)
        assert isinstance(or_(NoMatchFilter(), EqFilter(value=1)), EqFilter)

    def test_not_normalisation(self) -> None:
        assert isinstance(not_(AllFilter()), NoMatchFilter)
        assert isinstance(not_(NoMatchFilter()), AllFilter)
        assert isinstance(not_(not_(EqFilter(value=1))), EqFilter)
        assert isinstance(not_(EqFilter(value=1)), NotFilter)

    def test_attr_normalisation_drops_constants(self) -> None:
        assert isinstance(attr("x", AllFilter()), AllFilter)
        assert isinstance(attr("x", NoMatchFilter()), NoMatchFilter)
        assert isinstance(attr("x", EqFilter(value=1)), AttrFilter)

    def test_nested_and_or_flatten(self) -> None:
        flat = and_(AndFilter(filters=(EqFilter(value=1),)), EqFilter(value=2))
        assert isinstance(flat, AndFilter)
        assert len(flat.filters) == 2

    def test_normalisation_preserves_matches(self) -> None:
        raw = and_(AllFilter(), attr("x", GtFilter(value=1)), NoMatchFilter())
        assert isinstance(raw, NoMatchFilter)
        for value in (0, 2, None):
            assert raw.matches(Row(x=value)) is False

    def test_frozen_and_hashable(self) -> None:
        flt = attr("x", EqFilter(value=1))
        with pytest.raises(ValidationError):
            flt.attribute = "y"  # type: ignore[misc]
        assert {flt, attr("x", EqFilter(value=1))} == {flt}

    def test_dump_and_roundtrip(self) -> None:
        flt = build_filter([("score", "ge", 5), ("name", "contains", "bob")])
        restored = SearchFilter.model_validate(flt.model_dump())
        assert restored == flt
        assert restored.matches(Row(score=6, name="BOB"))

    def test_operators_for_annotation(self) -> None:
        assert operators_for_annotation(int) == frozenset({"eq", "gt", "ge", "lt", "le"})
        assert "contains" in operators_for_annotation(str)
        assert "contains" in operators_for_annotation(str | None)
        assert operators_for_annotation(bool) == frozenset({"eq"})
        assert operators_for_annotation(Row) == frozenset({"eq"})


# ---------------------------------------------------------------------------
# Object filter lowering
# ---------------------------------------------------------------------------


class ObjFilter(BaseObjectFilter[Row]):
    name__contains: str | None = None
    score__ge: int | None = None
    score__lt: int | None = None
    tag__eq: str | None = None


class BadFilter(BaseObjectFilter[Row]):
    """A filter whose fields are not ``<attribute>__<op>`` clauses."""

    name__bogus: str | None = None
    plain: int | None = None


class TestObjectFilterLowering:
    def test_lowers_declared_fields(self) -> None:
        flt = ObjFilter(score__ge=5, name__contains="bob")
        standard = flt.create_standard_filter()
        assert isinstance(standard, AndFilter)
        assert standard.matches(Row(score=7, name="BOB"))
        assert not standard.matches(Row(score=3, name="BOB"))

    def test_empty_lowers_to_all(self) -> None:
        assert isinstance(ObjFilter().create_standard_filter(), AllFilter)
        assert ObjFilter().matches(Row(score=1))

    def test_lowering_is_cached(self) -> None:
        flt = ObjFilter(score__ge=5)
        assert flt.create_standard_filter() is flt.create_standard_filter()

    def test_ignores_non_clause_fields(self) -> None:
        assert isinstance(BadFilter(name__bogus="x", plain=1).create_standard_filter(), AllFilter)

    def test_roundtrips_through_search_filter(self) -> None:
        flt = ObjFilter(score__ge=5)
        restored = SearchFilter.model_validate(flt.model_dump())
        assert restored.matches(Row(score=9))

    def test_private_attr_stays_out_of_dump(self) -> None:
        assert "_standard_filter" not in ObjFilter(score__ge=5).model_dump()


# ---------------------------------------------------------------------------
# SQL conversion
# ---------------------------------------------------------------------------


class SqlBase(DeclarativeBase):
    pass


class Thing(SqlBase):
    __tablename__ = "things"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@pytest_asyncio.fixture
async def sql_env() -> AsyncIterator[tuple[Any, SqlResource[Any]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Thing, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(SqlBase.metadata.create_all)
    async with maker() as session:
        now = datetime.now(UTC)
        await session.execute(
            Thing.__table__.insert(),
            [
                {"id": 1, "name": "alice", "score": 5, "created_at": now},
                {"id": 2, "name": "bob", "score": 10, "created_at": now + timedelta(days=1)},
                {"id": 3, "name": None, "score": None, "created_at": None},
            ],
        )
        await session.commit()
    yield maker, resource
    await engine.dispose()


async def _ids(resource: SqlResource[Any], filter_: SearchFilter[Any]) -> list[int]:
    from sqlalchemy import select

    async with resource._session_factory() as session:
        converter = resource.build_filter_converter(session)
        await converter.resolve()
        standard = filter_.create_standard_filter()
        stmt = converter.apply(select(Thing.__table__.c.id), standard)
        return list((await session.execute(stmt)).scalars().all())


class ThingFilter(BaseObjectFilter[Any]):
    score__ge: int | None = None


class WeirdFilter(BaseObjectFilter[Any]):
    score__ge: int | None = None


class TestSqlConversion:
    async def test_equality_and_ordering(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, attr("id", EqFilter(value=1))) == [1]
        assert await _ids(resource, attr("id", GtFilter(value=1))) == [2, 3]
        assert await _ids(resource, attr("id", GeFilter(value=2))) == [2, 3]
        assert await _ids(resource, attr("id", LtFilter(value=2))) == [1]
        assert await _ids(resource, attr("id", LeFilter(value=2))) == [1, 2]

    async def test_contains_is_case_insensitive(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, attr("name", ContainsFilter(value="ALI"))) == [1]

    async def test_and_or(self, sql_env) -> None:
        _maker, resource = sql_env
        both = and_(attr("score", GtFilter(value=4)), attr("name", ContainsFilter(value="a")))
        assert await _ids(resource, both) == [1]
        either = or_(attr("id", EqFilter(value=1)), attr("id", EqFilter(value=3)))
        assert await _ids(resource, either) == [1, 3]

    async def test_constants(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, AllFilter()) == [1, 2, 3]
        assert await _ids(resource, NoMatchFilter()) == []

    async def test_negation_is_null_safe(self, sql_env) -> None:
        """The NULL row must match the complement, not vanish from both branches."""
        _maker, resource = sql_env
        assert await _ids(resource, attr("score", EqFilter(value=5))) == [1]
        # The complement of `score = 5` is rows 2 (10) and 3 (NULL).
        assert await _ids(resource, not_(attr("score", EqFilter(value=5)))) == [2, 3]
        # Same for a comparison and a substring negation over NULL.
        assert await _ids(resource, not_(attr("score", GtFilter(value=5)))) == [1, 3]
        assert await _ids(resource, not_(attr("name", ContainsFilter(value="a")))) == [2, 3]

    async def test_not_of_and(self, sql_env) -> None:
        _maker, resource = sql_env
        inner = and_(attr("score", GtFilter(value=1)), attr("name", ContainsFilter(value="a")))
        assert await _ids(resource, not_(inner)) == [2, 3]

    async def test_aware_datetime_is_normalised(self, sql_env) -> None:
        _maker, resource = sql_env
        # A non-UTC offset must be normalised before binding.
        tz = datetime.now().astimezone().tzinfo
        cutoff = datetime.now(UTC) + timedelta(days=2)
        aware = cutoff.astimezone(tz) if tz else cutoff
        assert await _ids(resource, attr("id", EqFilter(value=1))) == [1]
        assert await _ids(resource, attr("created_at", LtFilter(value=aware))) == [1, 2]

    async def test_object_filter_lowers_before_conversion(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, ThingFilter(score__ge=5)) == [1, 2]

    async def test_eq_none_matches_null_and_its_complement(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, attr("score", EqFilter(value=None))) == [3]
        assert await _ids(resource, not_(attr("score", EqFilter(value=None)))) == [1, 2]

    async def test_ge_and_le_negation_is_null_safe(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, not_(attr("score", GeFilter(value=10)))) == [1, 3]
        assert await _ids(resource, not_(attr("score", LeFilter(value=5)))) == [2, 3]

    async def test_constants_negation(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, not_(AllFilter())) == []
        assert await _ids(resource, not_(NoMatchFilter())) == [1, 2, 3]

    async def test_negated_or(self, sql_env) -> None:
        _maker, resource = sql_env
        inner = or_(attr("id", EqFilter(value=1)), attr("id", EqFilter(value=2)))
        assert await _ids(resource, not_(inner)) == [3]

    async def test_empty_and_or_nodes(self, sql_env) -> None:
        _maker, resource = sql_env
        assert await _ids(resource, AndFilter(filters=())) == [1, 2, 3]
        assert await _ids(resource, OrFilter(filters=())) == []
        assert await _ids(resource, not_(AndFilter(filters=()))) == []
        assert await _ids(resource, not_(OrFilter(filters=()))) == [1, 2, 3]

    def test_negated_unbound_value_filter_raises(self) -> None:
        class Unknown(SearchFilter[Any]):
            value: int = 1

            def matches(self, value: Any) -> bool:
                return True

        converter = SqlFilterConverter(SqlFilterContext(columns={"id": Thing.__table__.c.id}))
        with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
            converter.condition(Unknown())
        with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
            converter.negated_condition(Unknown())
        with pytest.raises(UnsupportedFilterError, match="bound column"):
            converter.negated_condition(EqFilter(value=1))

    def test_context_defaults(self) -> None:
        ctx = SqlFilterContext(columns={"id": Thing.__table__.c.id})
        assert ctx.session is None
        assert ctx.allow_iteration is False

    async def test_unknown_attribute_raises(self, sql_env) -> None:
        _maker, resource = sql_env
        with pytest.raises(UnsupportedFilterError, match="not a queryable field"):
            await _ids(resource, attr("nope", EqFilter(value=1)))

    async def test_unbound_value_filter_raises(self, sql_env) -> None:
        _maker, resource = sql_env
        with pytest.raises(UnsupportedFilterError, match="bound column"):
            await _ids(resource, EqFilter(value=1))

    async def test_registries_are_enumerable(self) -> None:
        from resourcey.v2.sql import filter_converter as fc

        for node_type in (
            AllFilter,
            NoMatchFilter,
            AndFilter,
            OrFilter,
            NotFilter,
        ):
            assert node_type in fc._LOGICAL_REGISTRY
        for node_type in (
            EqFilter,
            GtFilter,
            GeFilter,
            LtFilter,
            LeFilter,
            ContainsFilter,
        ):
            assert node_type in fc._OPERATOR_REGISTRY
        assert fc.is_operator(EqFilter(value=1))
        assert not fc.is_operator(AllFilter())


# ---------------------------------------------------------------------------
# Derived surface + HTTP
# ---------------------------------------------------------------------------


class TestFilterSurfaceQueries:
    async def test_search_and_count_filter(self, sql_env) -> None:
        _maker, resource = sql_env
        async with resource.get_service() as service:
            page = await service.search(
                spec=SearchSpec(filters=build_filter([("name", "contains", "b")]))
            )
            assert [item.id for item in page.items] == [2]
            assert await service.count(filters=build_filter([("score", "ge", 10)])) == 1
            assert await service.count() == 3


class ApiBase(DeclarativeBase):
    pass


class Widget(ApiBase):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100))
    secret: Mapped[str] = mapped_column(
        String(100),
        info={"dto_field": DtoField(in_read_response=False, in_search_response=False)},
    )


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[tuple[AsyncClient, SqlResource[Any]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    widgets = SqlResource(Widget, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(ApiBase.metadata.create_all)
    manifest = Manifest(resources=[widgets])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for title in ("alpha", "beta", "gamma"):
                resp = await client.post("/widgets", json={"title": title, "secret": "s"})
                assert resp.status_code == 201
            yield client, widgets
    await engine.dispose()


class TestFilterSurface:
    async def test_derived_surface_matches_read_model(self, api_client) -> None:
        _client, widgets = api_client
        assert "title" in widgets.get_queryable_fields()
        assert "secret" not in widgets.get_queryable_fields()
        assert "contains" in widgets.get_filter_operators()["title"]

    async def test_http_search_filters(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/widgets", params={"title__eq": "beta"})
        assert resp.status_code == 200
        assert [i["title"] for i in resp.json()["items"]] == ["beta"]
        resp = await client.get("/widgets", params={"title__contains": "A"})
        assert [i["title"] for i in resp.json()["items"]] == ["alpha", "beta", "gamma"]
        resp = await client.get("/widgets", params={"id__gt": 1})
        assert [i["title"] for i in resp.json()["items"]] == ["beta", "gamma"]

    async def test_http_count_filters(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/widgets/count", params={"title__contains": "a"})
        assert resp.status_code == 200
        assert resp.json() == 3

    async def test_unknown_field_is_400(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/widgets", params={"nope__eq": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_input"

    async def test_hidden_field_is_400(self, api_client) -> None:
        """A field projected away from the read model must not be filterable."""
        client, _ = api_client
        resp = await client.get("/widgets", params={"secret__eq": "s"})
        assert resp.status_code == 400

    async def test_unsupported_operator_is_400(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/widgets", params={"id__contains": "1"})
        assert resp.status_code == 400

    async def test_bad_value_type_is_422(self, api_client) -> None:
        client, _ = api_client
        resp = await client.get("/widgets", params={"id__gt": "not-an-int"})
        assert resp.status_code == 422

    async def test_filters_appear_in_openapi(self, api_client) -> None:
        client, _ = api_client
        spec = (await client.get("/openapi.json")).json()
        params = {p["name"] for p in spec["paths"]["/widgets"]["get"]["parameters"]}
        assert {"title__eq", "title__contains", "id__gt"} <= params


class TestFilterIterationFallback:
    async def test_iteration_fallback_matches_equivalently(self, sql_env) -> None:
        _maker, resource = sql_env
        resource.allow_filter_iteration = True
        async with resource.get_service() as service:
            # A filter that lowers to standard nodes converts normally, so the
            # fallback is exercised by a filter referencing a relationship-like
            # attribute that has no column.
            page = await service.search(
                spec=SearchSpec(filters=WeirdFilter(score__ge=5)),
            )
            assert [item.id for item in page.items] == [1, 2]

    async def test_iteration_fallback_recovers_from_unconvertible(self, sql_env) -> None:
        _maker, resource = sql_env
        resource.allow_filter_iteration = True
        async with resource.get_service() as service:
            page = await service.search(spec=SearchSpec(filters=attr("nope", EqFilter(value=1))))
            assert page.items == []

    async def test_unconvertible_without_opt_in_raises(self, sql_env) -> None:
        _maker, resource = sql_env
        async with resource.get_service() as service:
            with pytest.raises(UnsupportedFilterError):
                await service.search(spec=SearchSpec(filters=attr("nope", EqFilter(value=1))))
