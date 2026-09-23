"""Tests for the read-only list-backed resource backend (issue #71).

Covers the read subset of the standard ``Action`` contract (read / search /
count / batch_read) at the service level, plus HTTP-level behaviour against a
real FastAPI app with the list routes registered: schemas, cursor pagination,
sort, declared filters, defensive cloning, and the structural guarantee that
**no write route is mounted**.

The list backend has no external dependency, so the tests exercise the real
production code path end to end (no mocks).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from resourcey.list.list_resource import ListResource
from resourcey.list.list_service import ListService
from resourcey.resource.errors import InvalidInputError, NotFoundError, ResourceyConfigError
from resourcey.resource.field import ResourceyField
from resourcey.resource.routes import register_error_handlers, register_routes
from resourcey.resource.service import Page
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.search_filter import BaseSearchFilter

# ---------------------------------------------------------------------------
# Test models + resources
# ---------------------------------------------------------------------------


class Country(BaseModel):
    """A Pydantic model that is already the resource's read model."""

    id: str
    name: str
    iso3: str
    population: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _countries() -> list[Country]:
    return [
        Country(id="us", name="United States", iso3="USA", population=331_000_000),
        Country(id="ca", name="Canada", iso3="CAN", population=38_000_000),
        Country(id="mx", name="Mexico", iso3="MEX", population=126_000_000),
    ]


def country_resource() -> ListResource:
    return ListResource(models=_countries(), path="countries")


class FilterableCountry(BaseModel):
    id: str
    name: str
    population: int = 0


class CountrySearchFilter(BaseSearchFilter[Any]):
    name__contains: str | None = None
    population__gte: int | None = None


class FilteredResource(ListResource):
    """A list resource opting into filtering via a declared search filter."""

    def __init__(self) -> None:
        super().__init__(
            models=[
                FilterableCountry(id="us", name="United States", population=331_000_000),
                FilterableCountry(id="ca", name="Canada", population=38_000_000),
                FilterableCountry(id="mx", name="Mexico", population=126_000_000),
            ],
            path="countries",
        )

    @classmethod
    def get_search_filter_type(cls) -> type[BaseSearchFilter] | None:  # type: ignore[override]
        return CountrySearchFilter


class UnsortableCountry(BaseModel):
    id: Annotated[int, ResourceyField(sortable=False)]


class UnsortableResource(ListResource):
    """A list resource whose only field is opted out of sorting."""

    def __init__(self) -> None:
        super().__init__(models=[UnsortableCountry(id=1)], path="unsortables")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_client(resource: ListResource) -> AsyncClient:
    """An httpx AsyncClient against an app with the resource's routes registered."""
    app = FastAPI()
    register_routes(app, resource)
    register_error_handlers(app)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _service(resource: ListResource | None = None) -> ListService:
    resource = resource or country_resource()
    return ListService(resource, items=list(resource._models))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestListResourceConstruction:
    def test_model_is_inferred_from_items(self) -> None:
        resource = country_resource()
        assert resource.get_read_model() is Country
        assert list(resource.model_fields) == ["id", "name", "iso3", "population", "created_at"]

    def test_path_defaults_to_model_name(self) -> None:
        resource = ListResource(models=[Country(id="us", name="US", iso3="USA")])
        assert resource.get_resource_path() == "countrys"

    def test_explicit_path_wins(self) -> None:
        assert country_resource().get_resource_path() == "countries"

    def test_defensive_is_default(self) -> None:
        assert country_resource()._defensive is True

    def test_empty_models_without_model_raises(self) -> None:
        with pytest.raises(ResourceyConfigError):
            ListResource(models=[])

    def test_empty_models_with_model_builds(self) -> None:
        resource = ListResource(model=Country, models=[], path="countries")
        assert resource.get_read_model() is Country

    def test_non_pydantic_model_raises(self) -> None:
        with pytest.raises(ResourceyConfigError):
            ListResource(model=dict, models=[], path="x")  # type: ignore[arg-type]

    def test_mixed_item_types_raise(self) -> None:
        with pytest.raises(ResourceyConfigError):
            ListResource(
                models=[
                    Country(id="us", name="US", iso3="USA"),
                    FilterableCountry(id="x", name="x"),
                ]
            )

    def test_on_register_requires_id(self) -> None:
        class NoId(BaseModel):
            name: str

        resource = ListResource(models=[NoId(name="x")], path="no-ids")
        with pytest.raises(ResourceyConfigError):
            resource.on_register()


# ---------------------------------------------------------------------------
# Action surface
# ---------------------------------------------------------------------------


class TestListActionSurface:
    def test_actions_are_read_only(self) -> None:
        assert country_resource().actions == frozenset(
            {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
        )

    def test_supported_actions_equal_actions(self) -> None:
        resource = country_resource()
        assert resource.get_supported_actions() == resource.actions

    def test_write_actions_not_supported(self) -> None:
        for action in (Action.CREATE, Action.UPDATE, Action.DELETE, Action.BATCH_EDIT):
            assert action not in country_resource().actions

    def test_base_service_declares_no_actions(self) -> None:
        assert not hasattr(BaseService, "actions")

    def test_create_model_is_unavailable(self) -> None:
        with pytest.raises(ResourceyConfigError):
            country_resource().get_create_model()

    def test_update_model_is_unavailable(self) -> None:
        with pytest.raises(ResourceyConfigError):
            country_resource().get_update_model()


# ---------------------------------------------------------------------------
# Defensive cloning
# ---------------------------------------------------------------------------


class TestDefensiveCloning:
    @pytest.mark.asyncio
    async def test_read_returns_a_clone(self) -> None:
        resource = country_resource()
        stored = resource._models[0]
        result = await ListService(resource, items=list(resource._models)).read("us")
        assert result is not stored
        assert result == stored

    @pytest.mark.asyncio
    async def test_mutating_a_result_does_not_touch_the_store(self) -> None:
        resource = country_resource()
        result = await ListService(resource, items=list(resource._models)).read("us")
        result.name = "Mutated"
        assert resource._models[0].name == "United States"

    @pytest.mark.asyncio
    async def test_search_results_are_clones(self) -> None:
        resource = country_resource()
        by_id = {item.id: item for item in resource._models}
        page = await ListService(resource, items=list(resource._models)).search(limit=10)
        assert all(item is not by_id[item.id] for item in page.items)

    @pytest.mark.asyncio
    async def test_batch_read_results_are_clones(self) -> None:
        resource = country_resource()
        by_id = {item.id: item for item in resource._models}
        results = await ListService(resource, items=list(resource._models)).batch_read(["us", "ca"])
        assert all(r is not by_id[r.id] for r in results)

    @pytest.mark.asyncio
    async def test_non_defensive_returns_the_stored_object(self) -> None:
        resource = ListResource(models=_countries(), defensive=False, path="countries")
        result = await ListService(resource, items=list(resource._models)).read("us")
        assert result is resource._models[0]

    def test_clone_for_output_identity_when_not_defensive(self) -> None:
        resource = ListResource(models=_countries(), defensive=False, path="countries")
        stored = resource._models[0]
        assert resource.clone_for_output(stored) is stored


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
            await _service(UnsortableResource()).search(sort="id")

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
        svc = _service(FilteredResource())
        page = await svc.search(limit=10, filters=CountrySearchFilter(name__contains="united"))
        assert [i.id for i in page.items] == ["us"]

    @pytest.mark.asyncio
    async def test_search_filter_and_sort_combined(self) -> None:
        svc = _service(FilteredResource())
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
        resource = country_resource()
        svc = ListService(resource, items=list(resource._models), serialization_context={"x": 1})
        assert svc.serialization_context() == {"x": 1}
        assert svc._ctx() == {"x": 1}


class TestListServiceCount:
    @pytest.mark.asyncio
    async def test_count_all(self) -> None:
        assert await _service().count() == 3

    @pytest.mark.asyncio
    async def test_count_with_filter(self) -> None:
        svc = _service(FilteredResource())
        assert await svc.count(filters=CountrySearchFilter(population__gte=100_000_000)) == 2

    @pytest.mark.asyncio
    async def test_count_empty(self) -> None:
        resource = ListResource(model=Country, models=[], path="countries")
        assert await ListService(resource, items=[]).count() == 0


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
            await _service().create(Country(id="fr", name="France", iso3="FRA"))

    @pytest.mark.asyncio
    async def test_update_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().update("us", Country(id="us", name="X", iso3="XXX"))

    @pytest.mark.asyncio
    async def test_delete_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().delete("us")

    @pytest.mark.asyncio
    async def test_batch_edit_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            await _service().batch_edit([("us", Country(id="us", name="X", iso3="XXX"))])


class TestListServiceProjection:
    @pytest.mark.asyncio
    async def test_mapping_items_are_projected(self) -> None:
        resource = ListResource(model=Country, models=[], path="countries")
        svc = ListService(resource, items=[{"id": "ca", "name": "Canada", "iso3": "CAN"}])
        result = await svc.read("ca")
        assert result.name == "Canada"

    @pytest.mark.asyncio
    async def test_plain_objects_are_projected(self) -> None:
        class Plain:
            def __init__(self) -> None:
                self.id = "x"
                self.name = "Plain"

        class Tiny(BaseModel):
            id: str
            name: str

        resource = ListResource(model=Tiny, models=[], path="tinies")
        result = await ListService(resource, items=[Plain()]).read("x")
        assert result.name == "Plain"

    @pytest.mark.asyncio
    async def test_source_list_not_mutated(self) -> None:
        source = [
            FilterableCountry(id="a", name="A"),
            FilterableCountry(id="b", name="B"),
        ]
        resource = ListResource(models=source, path="countries")
        await ListService(resource, items=list(resource._models)).search(limit=10, sort="name")
        assert [c.id for c in source] == ["a", "b"]
        assert source[0].name == "A"


# ---------------------------------------------------------------------------
# HTTP-level tests
# ---------------------------------------------------------------------------


class TestListHttpReadSearch:
    @pytest.mark.asyncio
    async def test_get_by_id(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries/us")
        assert resp.status_code == 200
        assert resp.json()["name"] == "United States"

    @pytest.mark.asyncio
    async def test_get_missing_returns_404_envelope(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries/zz")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_search_returns_page(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries", params={"limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert [i["id"] for i in body["items"]] == ["ca", "mx"]
        assert body["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_search_cursor_round_trip(self) -> None:
        async with _build_client(country_resource()) as client:
            first = await client.get("/countries", params={"limit": 2})
            cursor = first.json()["next_cursor"]
            second = await client.get("/countries", params={"limit": 2, "cursor": cursor})
        assert [i["id"] for i in second.json()["items"]] == ["us"]

    @pytest.mark.asyncio
    async def test_search_sort_param(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get(
                "/countries", params={"limit": 10, "sort": "population", "desc": "true"}
            )
        assert [i["id"] for i in resp.json()["items"]] == ["us", "mx", "ca"]

    @pytest.mark.asyncio
    async def test_search_bad_sort_returns_422(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries", params={"sort": "bogus"})
        assert resp.status_code == 422  # enum of sortable fields rejects it

    @pytest.mark.asyncio
    async def test_count_returns_integer(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries/count")
        assert resp.status_code == 200
        assert resp.json() == 3

    @pytest.mark.asyncio
    async def test_batch_read_query_params(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.get("/countries/batch-read", params=[("id", "us"), ("id", "zz")])
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["id"] == "us"
        assert body[1] is None

    @pytest.mark.asyncio
    async def test_filter_query_params(self) -> None:
        async with _build_client(FilteredResource()) as client:
            resp = await client.get("/countries", params={"name__contains": "united"})
        assert [i["id"] for i in resp.json()["items"]] == ["us"]

    @pytest.mark.asyncio
    async def test_http_result_is_defensive(self) -> None:
        resource = country_resource()
        async with _build_client(resource) as client:
            resp = await client.get("/countries/us")
        assert resp.status_code == 200
        assert resource._models[0].name == "United States"


class TestListHttpNoWriteRoutes:
    """A read-only resource must not mount write routes (structural)."""

    @pytest.mark.asyncio
    async def test_post_create_not_mounted(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.post("/countries", json={"id": "fr", "name": "France"})
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_patch_update_not_mounted(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.patch("/countries/us", json={"name": "X"})
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_delete_not_mounted(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.delete("/countries/us")
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_batch_edit_not_mounted(self) -> None:
        async with _build_client(country_resource()) as client:
            resp = await client.post("/countries/batch-edit", json=[{"id": "us"}])
        assert resp.status_code == 405


class TestListHttpOpenApiSchema:
    @pytest.mark.asyncio
    async def test_request_body_schema_absent_for_writes(self) -> None:
        app = FastAPI()
        register_routes(app, country_resource())
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
        app = FastAPI()
        register_routes(app, country_resource())
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

        manifest = ResourceManifest(resources=(country_resource(),))
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

    @pytest.mark.asyncio
    async def test_manifest_serves_read_through_lifecycle(self) -> None:
        from resourcey.manifest import ResourceManifest

        manifest = ResourceManifest(resources=(country_resource(),))
        app = manifest.create_app()
        async with (
            manifest,
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            count = await client.get("/countries/count")
            assert count.json() == 3
            page = await client.get("/countries", params={"limit": 1})
            assert [i["id"] for i in page.json()["items"]] == ["ca"]
