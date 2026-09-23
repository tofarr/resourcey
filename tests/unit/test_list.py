"""Tests for the read-only list-backed resource backend (issue #71).

Covers the read subset of the standard ``Action`` contract (read / search /
count / batch_read) at the service level, plus HTTP-level behaviour against a
real FastAPI app with the list routes registered: schemas, cursor pagination,
sort, declared filters, and the structural guarantee that **no write route is
mounted**.

The list backend has no external dependency, so the tests exercise the real
production code path end to end (no mocks).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field

from resourcey.list.list_resource import ListResource
from resourcey.list.list_service import ListService
from resourcey.resource.errors import InvalidInputError, NotFoundError, ResourceyConfigError
from resourcey.resource.field import ResourceyField
from resourcey.resource.routes import register_error_handlers, register_routes
from resourcey.resource.service import Page
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.search_filter import BaseSearchFilter

# ---------------------------------------------------------------------------
# Test resources
# ---------------------------------------------------------------------------


class Country(ListResource):
    """A read-only resource backed by a list of Pydantic read-model objects."""

    id: str
    name: str
    iso3: str
    population: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def get_items(self) -> Any:
        read = self.get_read_model()
        return [
            read(id="us", name="United States", iso3="USA", population=331_000_000),
            read(id="ca", name="Canada", iso3="CAN", population=38_000_000),
            read(id="mx", name="Mexico", iso3="MEX", population=126_000_000),
        ]

    def get_resource_path(self) -> str:
        return "countries"


class FilterableCountry(ListResource):
    """A list resource opting into filtering via a declared search filter."""

    id: str
    name: str
    population: int = 0

    def get_items(self) -> Any:
        return [
            {"id": "us", "name": "United States", "population": 331_000_000},
            {"id": "ca", "name": "Canada", "population": 38_000_000},
            {"id": "mx", "name": "Mexico", "population": 126_000_000},
        ]

    @classmethod
    def get_search_filter_type(cls) -> type[BaseSearchFilter] | None:  # type: ignore[override]
        return CountrySearchFilter

    def get_resource_path(self) -> str:
        return "countries"


class CountrySearchFilter(BaseSearchFilter[Any]):
    name__contains: str | None = None
    population__gte: int | None = None


class AsyncCountry(ListResource):
    """A list resource whose ``get_items`` is async (data from any async source)."""

    id: str
    name: str

    async def get_items(self) -> Any:
        return [{"id": "nz", "name": "New Zealand"}, {"id": "au", "name": "Australia"}]

    def get_resource_path(self) -> str:
        return "async-countries"


class NoDataResource(ListResource):
    """A list resource without a ``get_items`` override (config error on use)."""

    id: str
    name: str


class ExplodingWriteResource(ListResource):
    """Sanity check that narrowing is on ``actions``, not just routing."""

    id: int
    label: str = "x"

    def get_items(self) -> Any:
        return [{"id": 1, "label": "a"}]


class UnsortableList(ListResource):
    """A list resource whose only field is opted out of sorting."""

    id: Annotated[int, ResourceyField(sortable=False)]

    def get_items(self) -> Any:
        return [{"id": 1}]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_client(resource_type: type[ListResource]) -> AsyncClient:
    """An httpx AsyncClient against an app with the resource's routes registered."""
    resource = resource_type()
    app = FastAPI()
    register_routes(app, resource)
    register_error_handlers(app)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _service(resource_type: type[ListResource] = Country) -> ListService:
    resource = resource_type()
    return ListService(resource, items=resource.get_items())


# ---------------------------------------------------------------------------
# Action surface
# ---------------------------------------------------------------------------


class TestListActionSurface:
    def test_actions_are_read_only(self) -> None:
        assert Country().actions == frozenset(
            {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
        )

    def test_supported_actions_equal_actions(self) -> None:
        assert Country().get_supported_actions() == Country().actions

    def test_write_actions_not_supported(self) -> None:
        for action in (Action.CREATE, Action.UPDATE, Action.DELETE, Action.BATCH_EDIT):
            assert action not in Country().actions

    def test_base_service_declares_no_actions(self) -> None:
        assert not hasattr(BaseService, "actions")


# ---------------------------------------------------------------------------
# Service-level read subset
# ---------------------------------------------------------------------------


class TestListServiceRead:
    @pytest.mark.asyncio
    async def test_read_returns_item(self) -> None:
        result = await _service().read("us")
        assert result.id == "us"
        assert result.name == "United States"

    @pytest.mark.asyncio
    async def test_read_missing_raises_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            await _service().read("zz")


class TestListServiceSearch:
    @pytest.mark.asyncio
    async def test_search_returns_page_with_metadata(self) -> None:
        page = await _service().search(limit=10)
        assert isinstance(page, Page)
        assert len(page.items) == 3
        assert page.limit == 10
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_paginates_with_cursor(self) -> None:
        svc = _service()
        page1 = await svc.search(limit=2)
        assert [i.id for i in page1.items] == ["ca", "mx"]
        page2 = await svc.search(limit=2, cursor=page1.next_cursor)
        assert [i.id for i in page2.items] == ["us"]
        assert page2.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_sort_ascending_and_descending(self) -> None:
        svc = _service()
        asc = await svc.search(limit=10, sort="population")
        assert [i.id for i in asc.items] == ["ca", "mx", "us"]
        desc = await svc.search(limit=10, sort="population", desc=True)
        assert [i.id for i in desc.items] == ["us", "mx", "ca"]

    @pytest.mark.asyncio
    async def test_search_rejects_unknown_sort(self) -> None:
        with pytest.raises(InvalidInputError):
            await _service().search(sort="nope")

    @pytest.mark.asyncio
    async def test_search_rejects_non_sortable_field(self) -> None:
        with pytest.raises(InvalidInputError):
            await _service(UnsortableList).search(sort="id")

    @pytest.mark.asyncio
    async def test_search_rejects_cursor_from_different_sort(self) -> None:
        svc = _service()
        page = await svc.search(limit=1, sort="population")
        assert page.next_cursor is not None
        with pytest.raises(InvalidInputError):
            await svc.search(limit=1, cursor=page.next_cursor)  # no sort

    @pytest.mark.asyncio
    async def test_search_rejects_invalid_cursor(self) -> None:
        with pytest.raises(InvalidInputError):
            await _service().search(cursor="not-a-cursor")

    @pytest.mark.asyncio
    async def test_search_rejects_non_positive_limit(self) -> None:
        with pytest.raises(InvalidInputError):
            await _service().search(limit=0)

    @pytest.mark.asyncio
    async def test_search_caps_limit(self) -> None:
        page = await _service().search(limit=10_000)
        assert page.limit == 100

    @pytest.mark.asyncio
    async def test_search_with_filter(self) -> None:
        svc = _service(FilterableCountry)
        page = await svc.search(limit=10, filters=CountrySearchFilter(name__contains="united"))
        assert [i.id for i in page.items] == ["us"]

    @pytest.mark.asyncio
    async def test_search_filter_and_sort_combined(self) -> None:
        svc = _service(FilterableCountry)
        page = await svc.search(
            limit=10,
            sort="population",
            desc=True,
            filters=CountrySearchFilter(population__gte=100_000_000),
        )
        assert [i.id for i in page.items] == ["us", "mx"]

    @pytest.mark.asyncio
    async def test_search_desc_sort_paginates_with_cursor(self) -> None:
        svc = _service()
        page1 = await svc.search(limit=2, sort="population", desc=True)
        assert [i.id for i in page1.items] == ["us", "mx"]
        page2 = await svc.search(limit=2, sort="population", desc=True, cursor=page1.next_cursor)
        assert [i.id for i in page2.items] == ["ca"]
        assert page2.next_cursor is None

    @pytest.mark.asyncio
    async def test_search_no_sort_cursor_uses_id_only(self) -> None:
        svc = _service()
        page1 = await svc.search(limit=2)
        assert [i.id for i in page1.items] == ["ca", "mx"]
        page2 = await svc.search(limit=2, cursor=page1.next_cursor)
        assert [i.id for i in page2.items] == ["us"]

    @pytest.mark.asyncio
    async def test_search_desc_no_sort_cursor_uses_id_only(self) -> None:
        svc = _service()
        page1 = await svc.search(limit=2, sort="id", desc=True)
        assert [i.id for i in page1.items] == ["us", "mx"]
        page2 = await svc.search(limit=2, sort="id", desc=True, cursor=page1.next_cursor)
        assert [i.id for i in page2.items] == ["ca"]

    @pytest.mark.asyncio
    async def test_serialization_context_flows_to_items(self) -> None:
        svc = ListService(Country(), items=Country().get_items(), serialization_context={"x": 1})
        assert svc.serialization_context() == {"x": 1}
        assert svc._ctx() == {"x": 1}


class TestListServiceCount:
    @pytest.mark.asyncio
    async def test_count_all(self) -> None:
        assert await _service().count() == 3

    @pytest.mark.asyncio
    async def test_count_with_filter(self) -> None:
        svc = _service(FilterableCountry)
        assert await svc.count(filters=CountrySearchFilter(population__gte=100_000_000)) == 2

    @pytest.mark.asyncio
    async def test_count_empty(self) -> None:
        class Empty(ListResource):
            id: str

            def get_items(self) -> Any:
                return []

        assert await _service(Empty).count() == 0


class TestListServiceBatchRead:
    @pytest.mark.asyncio
    async def test_batch_read_preserves_order(self) -> None:
        result = await _service().batch_read(["us", "ca"])
        assert [r.id for r in result] == ["us", "ca"]

    @pytest.mark.asyncio
    async def test_batch_read_inserts_null_for_absent(self) -> None:
        result = await _service().batch_read(["us", "zz"])
        assert result[0].id == "us"
        assert result[1] is None

    @pytest.mark.asyncio
    async def test_batch_read_empty(self) -> None:
        assert await _service().batch_read([]) == []

    @pytest.mark.asyncio
    async def test_batch_read_deduplicates(self) -> None:
        result = await _service().batch_read(["us", "us"])
        assert [r.id for r in result] == ["us", "us"]


class TestListWriteActionsUnimplemented:
    @pytest.mark.asyncio
    async def test_create_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().create(Country().get_create_model()(name="France", iso3="FRA"))

    @pytest.mark.asyncio
    async def test_update_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().update("us", Country().get_update_model()())

    @pytest.mark.asyncio
    async def test_delete_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().delete("us")

    @pytest.mark.asyncio
    async def test_batch_edit_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().batch_edit([("us", Country().get_update_model()())])


class TestListServiceDataShapes:
    @pytest.mark.asyncio
    async def test_mapping_items_are_projected(self) -> None:
        svc = _service(FilterableCountry)
        result = await svc.read("ca")
        assert result.name == "Canada"

    @pytest.mark.asyncio
    async def test_plain_objects_are_projected(self) -> None:
        class Plain:
            def __init__(self) -> None:
                self.id = "x"
                self.name = "Plain"

        class PlainResource(ListResource):
            id: str
            name: str

            def get_items(self) -> Any:
                return [Plain()]

        result = await _service(PlainResource).read("x")
        assert result.name == "Plain"

    @pytest.mark.asyncio
    async def test_source_list_not_mutated(self) -> None:
        source = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]

        class Res(ListResource):
            id: str
            name: str

            def get_items(self) -> Any:
                return source

        svc = _service(Res)
        await svc.search(limit=10, sort="name")
        assert source == [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]


class TestListGetItemsErrors:
    def test_missing_get_items_raises_config_error(self) -> None:
        with pytest.raises(ResourceyConfigError):
            NoDataResource().get_items()


# ---------------------------------------------------------------------------
# HTTP-level tests
# ---------------------------------------------------------------------------


class TestListHttpReadSearch:
    @pytest.mark.asyncio
    async def test_get_by_id(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries/us")
        assert resp.status_code == 200
        assert resp.json()["name"] == "United States"

    @pytest.mark.asyncio
    async def test_get_missing_returns_404_envelope(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries/zz")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_search_returns_page(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries", params={"limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert [i["id"] for i in body["items"]] == ["ca", "mx"]
        assert body["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_search_cursor_round_trip(self) -> None:
        async with _build_client(Country) as client:
            first = await client.get("/countries", params={"limit": 2})
            cursor = first.json()["next_cursor"]
            second = await client.get("/countries", params={"limit": 2, "cursor": cursor})
        assert [i["id"] for i in second.json()["items"]] == ["us"]

    @pytest.mark.asyncio
    async def test_search_sort_param(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get(
                "/countries", params={"limit": 10, "sort": "population", "desc": "true"}
            )
        assert [i["id"] for i in resp.json()["items"]] == ["us", "mx", "ca"]

    @pytest.mark.asyncio
    async def test_search_bad_sort_returns_400(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries", params={"sort": "bogus"})
        assert resp.status_code == 422  # enum of sortable fields rejects it

    @pytest.mark.asyncio
    async def test_count_returns_integer(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries/count")
        assert resp.status_code == 200
        assert resp.json() == 3

    @pytest.mark.asyncio
    async def test_batch_read_query_params(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.get("/countries/batch-read", params=[("id", "us"), ("id", "zz")])
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["id"] == "us"
        assert body[1] is None

    @pytest.mark.asyncio
    async def test_filter_query_params(self) -> None:
        async with _build_client(FilterableCountry) as client:
            resp = await client.get("/countries", params={"name__contains": "united"})
        assert [i["id"] for i in resp.json()["items"]] == ["us"]

    @pytest.mark.asyncio
    async def test_async_get_items(self) -> None:
        async with _build_client(AsyncCountry) as client:
            resp = await client.get("/async-countries")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2


class TestListHttpNoWriteRoutes:
    """A read-only resource must not mount write routes (structural)."""

    @pytest.mark.asyncio
    async def test_post_create_not_mounted(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.post("/countries", json={"id": "fr", "name": "France"})
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_patch_update_not_mounted(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.patch("/countries/us", json={"name": "X"})
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_delete_not_mounted(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.delete("/countries/us")
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_batch_edit_not_mounted(self) -> None:
        async with _build_client(Country) as client:
            resp = await client.post("/countries/batch-edit", json=[{"id": "us"}])
        assert resp.status_code == 405


class TestListHttpOpenApiSchema:
    @pytest.mark.asyncio
    async def test_request_body_schema_absent_for_writes(self) -> None:
        resource = Country()
        app = FastAPI()
        register_routes(app, resource)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            schema = (await client.get("/openapi.json")).json()
        paths = schema["paths"]
        # Read routes exist; no create/update/delete operations are documented.
        assert "post" not in paths["/countries"]
        assert "get" in paths["/countries"]
        assert "patch" not in paths["/countries/{id}"]
        assert "delete" not in paths["/countries/{id}"]


class TestListOpenApiSortEnum:
    @pytest.mark.asyncio
    async def test_sort_enum_lists_sortable_fields(self) -> None:
        resource = Country()
        app = FastAPI()
        register_routes(app, resource)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            schema = (await client.get("/openapi.json")).json()
        params = schema["paths"]["/countries"]["get"]["parameters"]
        sort_param = next(p for p in params if p["name"] == "sort")
        # The sort param is an enum of the resource's sortable fields, emitted
        # as a named component schema referenced by the parameter.
        ref = sort_param["schema"]["anyOf"][0]["$ref"]
        component = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        assert set(component["enum"]) == {
            "id",
            "name",
            "iso3",
            "population",
            "created_at",
        }


class TestListManifestIntegration:
    """A list resource works through the manifest/app assembly path."""

    @pytest.mark.asyncio
    async def test_manifest_serves_list_resource(self) -> None:
        from resourcey.manifest import ResourceManifest

        manifest = ResourceManifest(resources=(Country,))
        app = manifest.create_app()
        async with (
            manifest,
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            resp = await client.get("/countries/us")
            assert resp.status_code == 200
            assert resp.json()["name"] == "United States"
            # No write route is mounted through the manifest path either.
            post = await client.post("/countries", json={"name": "X", "iso3": "XXX"})
            assert post.status_code == 405
