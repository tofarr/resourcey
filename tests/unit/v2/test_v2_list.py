"""Tests for the ``v2`` list-backed (read-only) resource (issue #116).

The list backend is the third ``v2`` storage backend: a resource built from an
application-supplied list of Pydantic models and served read-only. It has no
external dependency, so these tests exercise the real production code path end
to end (no mocks): DTO inference, the read subset of the action contract
(``read`` / ``search`` / ``count`` / ``batch_read``), filter / sort / cursor
paging, defensive cloning, the query-surface gate, the inherited cache policy,
and the structural guarantee that **no write route is mounted**.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from resourcey.v2.cache.cache_strategy import OptimisticCacheStrategy
from resourcey.v2.core.dto import DTO, DtoField
from resourcey.v2.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.service import Action, NotFoundError, ServiceError
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.http.app import create_app
from resourcey.v2.list.list_resource import ListResource
from resourcey.v2.list.pydantic_2_dto import pydantic_2_dto
from resourcey.v2.util.search_filter import build_filter
from resourcey.v2.util.sort_order import AttrSortOrder

# ---------------------------------------------------------------------------
# Test models + helpers
# ---------------------------------------------------------------------------


class Country(BaseModel):
    """A plain Pydantic model that is already the served read model."""

    id: str
    name: str
    iso3: str
    population: int = 0


def _countries() -> list[Country]:
    return [
        Country(id="us", name="United States", iso3="USA", population=331_000_000),
        Country(id="ca", name="Canada", iso3="CAN", population=38_000_000),
        Country(id="mx", name="Mexico", iso3="MEX", population=126_000_000),
    ]


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-cursors")
        )
    )


def _resource(**kwargs: Any) -> ListResource[Any, Any]:
    return ListResource(_countries(), path="countries", encryption_service=_encryption(), **kwargs)


def _client(manifest: Manifest) -> AsyncClient:
    app = create_app(manifest)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# pydantic_2_dto — model -> DTO inference
# ---------------------------------------------------------------------------


class TestPydanticToDto:
    def test_uses_the_model_name_by_default_and_allows_an_override(self) -> None:
        assert pydantic_2_dto(Country).__name__ == "Country"
        assert pydantic_2_dto(Country, name="Renamed").__name__ == "Renamed"

    def test_keeps_field_order_and_annotations(self) -> None:
        dto = pydantic_2_dto(Country)
        assert list(dto.__dto_fields__) == ["id", "name", "iso3", "population"]

    def test_id_field_defaults_to_id(self) -> None:
        assert pydantic_2_dto(Country).id_field_name == "id"

    def test_custom_id_field_name(self) -> None:
        class Keyed(BaseModel):
            code: str
            label: str

        dto = pydantic_2_dto(Keyed, id_field_name="code")
        assert dto.id_field_name == "code"
        # A custom identifier is the author's natural key: still immutable, but
        # supplied by the client on create.
        fields = dto.get_fields()
        assert fields["code"].in_update_request is False
        assert fields["code"].in_create_request is True

    def test_missing_id_field_raises_at_declaration(self) -> None:
        class NoId(BaseModel):
            label: str

        with pytest.raises(TypeError):
            pydantic_2_dto(NoId)

    def test_nullability_is_preserved(self) -> None:
        class Optional(BaseModel):
            id: str
            nickname: str | None = None

        fields = {
            name: ann for name, (ann, _cfg) in pydantic_2_dto(Optional).__dto_fields__.items()
        }
        assert (fields["nickname"] | None) == (str | None)

    def test_annotated_dto_field_is_honoured_verbatim(self) -> None:
        class Hidden(BaseModel):
            id: str
            secret: Annotated[str, DtoField(in_read_response=False)]

        dto = pydantic_2_dto(Hidden)
        assert dto.get_fields()["secret"].in_read_response is False

    def test_json_schema_extra_dto_field_is_honoured(self) -> None:
        class Hidden(BaseModel):
            id: str
            secret: str = Field(json_schema_extra={"dto_field": DtoField(in_read_response=False)})

        dto = pydantic_2_dto(Hidden)
        assert dto.get_fields()["secret"].in_read_response is False

    def test_bare_fields_take_the_conventions(self) -> None:
        dto = pydantic_2_dto(Country)
        # The conventional ``id`` is server-generated, so it is not client input.
        assert dto.get_fields()["id"].in_create_request is False
        assert dto.get_fields()["id"].in_update_request is False

    def test_uuid_id_gets_a_server_side_factory(self) -> None:
        class Keyed(BaseModel):
            id: UUID
            name: str

        dto = pydantic_2_dto(Keyed)
        assert dto.get_fields()["id"].default_factory_for_create is uuid4


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestListResourceConstruction:
    def test_model_is_inferred_from_items(self) -> None:
        resource = _resource()
        assert resource.model is Country
        assert resource.get_dto_type() is Country

    def test_path_defaults_to_model_name(self) -> None:
        resource = ListResource([Country(id="us", name="US", iso3="USA")])
        assert resource.get_resource_path() == "countrys"

    def test_explicit_path_wins(self) -> None:
        assert _resource().get_resource_path() == "countries"

    def test_defensive_is_default(self) -> None:
        assert _resource().defensive is True

    def test_empty_models_without_model_raises(self) -> None:
        with pytest.raises(ResourceyConfigError):
            ListResource(models=[])

    def test_empty_models_with_model_builds(self) -> None:
        resource = ListResource(model=Country, models=[], path="countries")
        assert resource.model is Country

    def test_non_pydantic_model_raises(self) -> None:
        with pytest.raises(ResourceyConfigError):
            ListResource(model=dict, models=[], path="x")  # type: ignore[arg-type]

    def test_mixed_item_types_raise(self) -> None:
        class Other(BaseModel):
            id: str

        with pytest.raises(ResourceyConfigError):
            ListResource(models=[Country(id="us", name="US", iso3="USA"), Other(id="x")])

    def test_list_is_held_by_reference(self) -> None:
        countries = _countries()
        resource = ListResource(countries, path="countries")
        countries.append(Country(id="br", name="Brazil", iso3="BRA"))
        assert any(c.id == "br" for c in resource.models)

    def test_explicit_dto_wins_over_inference(self) -> None:
        class Handwritten(DTO):
            id: str
            label: str

        resource = ListResource([], model=Country, dto=Handwritten, path="countries")
        assert resource.get_dto_declaration() is Handwritten
        assert resource.get_id_field() == "id"

    def test_dto_only_resource_without_a_model(self) -> None:
        class Handwritten(DTO):
            id: str
            label: str

        resource = ListResource(dto=Handwritten, path="countries")
        assert resource.model is None
        assert resource.get_dto_type() is Handwritten.get_dto_type()

    def test_json_schema_extra_without_a_dto_field_is_ignored(self) -> None:
        class Note(BaseModel):
            id: str
            note: str = Field(json_schema_extra={"other": "value"})

        dto = pydantic_2_dto(Note)
        assert dto.get_fields()["note"].in_read_response is True

    def test_declared_sort_order_type_is_honoured(self) -> None:
        class Declared(ListResource[Any, Any]):
            def get_sort_order_type(self) -> type[Any] | None:
                return AttrSortOrder

        resource = Declared(_countries(), path="countries", encryption_service=_encryption())
        order = resource.resolve_sort_order("population", True)
        assert order == AttrSortOrder(attribute="population", descending=True)


# ---------------------------------------------------------------------------
# Action surface (read-only)
# ---------------------------------------------------------------------------


class TestListActionSurface:
    def test_actions_are_read_only(self) -> None:
        assert _resource().get_supported_actions() == frozenset(
            {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
        )

    def test_write_actions_not_supported(self) -> None:
        supported = _resource().get_supported_actions()
        for action in (Action.CREATE, Action.UPDATE, Action.DELETE, Action.BATCH_EDIT):
            assert action not in supported

    def test_manifest_accepts_the_read_only_action_set(self) -> None:
        Manifest(resources=(_resource(),))


# ---------------------------------------------------------------------------
# Service-level read subset
# ---------------------------------------------------------------------------


class TestListServiceRead:
    async def test_read_returns_the_item(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            found = await service.read("us")
        assert found.id == "us"
        assert found.name == "United States"

    async def test_read_missing_raises_not_found(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            with pytest.raises(NotFoundError):
                await service.read("zz")

    async def test_service_raises_before_enter(self) -> None:
        resource = _resource()
        service = await resource.get_service()
        with pytest.raises(ServiceError, match="before entering"):
            await service.read("us")

    async def test_count_all_and_filtered(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            assert await service.count() == 3
            assert await service.count(build_filter([("iso3", "eq", "CAN")])) == 1

    async def test_batch_read_aligns_positionally(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            found = await service.batch_read(["mx", "zz", "us"])
        assert [item.id if item else None for item in found] == ["mx", None, "us"]

    async def test_batch_read_empty(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            assert await service.batch_read([]) == []

    async def test_batch_read_duplicates(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            found = await service.batch_read(["us", "us"])
        assert [item.id for item in found] == ["us", "us"]

    async def test_ctx_seeded_items_win(self) -> None:
        resource = _resource()
        seeded = [Country(id="fr", name="France", iso3="FRA")]
        async with await resource.get_service({}) as _:
            pass
        async with await resource.get_service({"unused": 1}) as service:
            assert await service.count() == 3
        # A ctx carrying the storage key is adopted instead of the resource's list.
        from resourcey.v2.core.service import STORAGE_KEY

        async with await resource.get_service({STORAGE_KEY: seeded}) as service:
            assert await service.count() == 1
            assert (await service.read("fr")).name == "France"


# ---------------------------------------------------------------------------
# Filtering + sorting
# ---------------------------------------------------------------------------


class TestListFilteringSorting:
    async def test_search_filters_via_matches(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            page = await service.search(
                search_filter=build_filter([("name", "contains", "an")]), limit=10
            )
        assert [item.id for item in page.items] == ["ca"]

    async def test_search_orders_by_sort(self) -> None:
        resource = _resource()
        order = resource.resolve_sort_order("population", False)
        async with await resource.get_service() as service:
            page = await service.search(sort_order=order, limit=10)
        assert [item.id for item in page.items] == ["ca", "mx", "us"]

    async def test_search_descending(self) -> None:
        resource = _resource()
        order = resource.resolve_sort_order("population", True)
        async with await resource.get_service() as service:
            page = await service.search(sort_order=order, limit=10)
        assert [item.id for item in page.items] == ["us", "mx", "ca"]

    def test_resolve_sort_order_rejects_unknown(self) -> None:
        with pytest.raises(InvalidInputError):
            _resource().resolve_sort_order("secret", False)

    def test_resolve_sort_order_returns_none_without_sort(self) -> None:
        assert _resource().resolve_sort_order(None, True) is None


# ---------------------------------------------------------------------------
# Cursor paging
# ---------------------------------------------------------------------------


class TestListCursorPaging:
    async def _walk(self, resource: ListResource[Any, Any], limit: int, **kwargs: Any) -> list[str]:
        seen: list[str] = []
        cursor: str | None = None
        async with await resource.get_service() as service:
            while True:
                page = await service.search(limit=limit, cursor=cursor, **kwargs)
                seen.extend(item.id for item in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        return seen

    async def test_paging_walks_every_item_with_no_gaps_or_repeats(self) -> None:
        seen = await self._walk(_resource(), limit=1)
        assert sorted(seen) == ["ca", "mx", "us"]
        assert len(seen) == len(set(seen))

    async def test_last_page_has_no_next_cursor(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            page = await service.search(limit=10)
        assert len(page.items) == 3
        assert page.next_cursor is None

    async def test_paging_under_a_sort(self) -> None:
        resource = _resource()
        order = resource.resolve_sort_order("population", True)
        seen = await self._walk(resource, limit=1, sort_order=order)
        assert seen == ["us", "mx", "ca"]

    async def test_cursor_sort_mismatch_is_rejected(self) -> None:
        resource = _resource()
        ascending = resource.resolve_sort_order("population", False)
        descending = resource.resolve_sort_order("population", True)
        async with await resource.get_service() as service:
            page = await service.search(sort_order=ascending, limit=1)
            assert page.next_cursor is not None
            with pytest.raises(InvalidInputError):
                await service.search(sort_order=descending, limit=1, cursor=page.next_cursor)

    async def test_garbage_cursor_is_rejected(self) -> None:
        resource = _resource()
        async with await resource.get_service() as service:
            with pytest.raises(InvalidInputError):
                await service.search(cursor="not-a-jwe-token", limit=1)

    async def test_cursor_direction_mismatch_is_rejected(self) -> None:
        # A cursor built for the default (identifier) order is rejected when a
        # sort is requested.
        resource = _resource()
        order = resource.resolve_sort_order("population", False)
        async with await resource.get_service() as service:
            page = await service.search(limit=1)
            assert page.next_cursor is not None
            with pytest.raises(InvalidInputError):
                await service.search(sort_order=order, limit=1, cursor=page.next_cursor)

    async def test_cursor_on_the_identifier_itself(self) -> None:
        # Sorting explicitly by the identifier takes the same collapsed branch
        # as the default order.
        resource = _resource()
        order = resource.resolve_sort_order("id", False)
        async with await resource.get_service() as service:
            first = await service.search(sort_order=order, limit=1)
            second = await service.search(sort_order=order, limit=10, cursor=first.next_cursor)
        assert [item.id for item in second.items] == ["mx", "us"]

    async def test_cursor_descending_on_the_identifier(self) -> None:
        resource = _resource()
        order = resource.resolve_sort_order("id", True)
        async with await resource.get_service() as service:
            first = await service.search(sort_order=order, limit=1)
            second = await service.search(sort_order=order, limit=10, cursor=first.next_cursor)
        assert [item.id for item in first.items] == ["us"]
        assert [item.id for item in second.items] == ["mx", "ca"]


class TestExplicitDtoProjection:
    async def test_explicit_dto_projects_the_served_model(self) -> None:
        # The ``dto=`` escape hatch serves a hand-written declaration: the served
        # Pydantic model differs from the DTO type, so the field-by-field
        # projection runs.
        class Handwritten(DTO):
            id: str
            name: str

        resource = ListResource(
            _countries(),
            model=Country,
            dto=Handwritten,
            path="countries",
            encryption_service=_encryption(),
        )
        async with await resource.get_service() as service:
            found = await service.read("ca")
        assert isinstance(found, Handwritten.get_dto_type())
        assert found.name == "Canada"
        assert not hasattr(found, "iso3")


class TestNullableSortKeys:
    class Nullable(BaseModel):
        id: str
        label: str
        score: int | None = None

    def _nullable(self) -> ListResource[Any, Any]:
        items = [
            self.Nullable(id="a", label="a", score=None),
            self.Nullable(id="b", label="b", score=2),
            self.Nullable(id="c", label="c", score=1),
            self.Nullable(id="d", label="d", score=None),
        ]
        return ListResource(items, path="nullables", encryption_service=_encryption())

    async def test_paging_a_nullable_column_visits_every_row(self) -> None:
        resource = self._nullable()
        for descending in (False, True):
            order = resource.resolve_sort_order("score", descending)
            seen: list[str] = []
            cursor: str | None = None
            async with await resource.get_service() as service:
                while True:
                    page = await service.search(limit=1, cursor=cursor, sort_order=order)
                    seen.extend(item.label for item in page.items)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
            expected = ["a", "d", "c", "b"] if not descending else ["b", "c", "a", "d"]
            assert seen == expected

    async def test_null_cursor_key_round_trips(self) -> None:
        resource = self._nullable()
        order = resource.resolve_sort_order("score", False)
        async with await resource.get_service() as service:
            page = await service.search(sort_order=order, limit=1)
        assert page.next_cursor is not None
        from resourcey.v2.util.cursor import decode_cursor

        _field, _asc, sort_key, _id = decode_cursor(_encryption(), page.next_cursor)
        assert sort_key is None


# ---------------------------------------------------------------------------
# Defensive cloning
# ---------------------------------------------------------------------------


class Nested(BaseModel):
    x: int


class Container(BaseModel):
    id: str
    inner: Nested
    items: list[int]
    blob: dict[str, int]


class TestDefensiveCloning:
    def _resource(self, *, defensive: bool) -> ListResource[Any, Any]:
        stored = Container(id="a", inner=Nested(x=1), items=[1, 2], blob={"k": 1})
        return ListResource([stored], path="containers", defensive=defensive)

    async def test_defensive_read_is_isolated(self) -> None:
        resource = self._resource(defensive=True)
        stored = resource.models[0]
        async with await resource.get_service() as service:
            found = await service.read("a")
            assert found is not stored
            assert found == stored
            found.inner.x = 999
            found.items.append(3)
            found.blob["k"] = 999
        assert stored.inner.x == 1
        assert stored.items == [1, 2]
        assert stored.blob == {"k": 1}

    async def test_defensive_search_results_are_isolated(self) -> None:
        resource = self._resource(defensive=True)
        stored = resource.models[0]
        async with await resource.get_service() as service:
            page = await service.search(limit=10)
        assert page.items[0] is not stored

    async def test_defensive_batch_read_results_are_isolated(self) -> None:
        resource = self._resource(defensive=True)
        stored = resource.models[0]
        async with await resource.get_service() as service:
            found = await service.batch_read(["a"])
        assert found[0] is not stored

    async def test_non_defensive_serves_the_stored_object(self) -> None:
        resource = self._resource(defensive=False)
        stored = resource.models[0]
        async with await resource.get_service() as service:
            found = await service.read("a")
        assert found is stored

    def test_clone_for_output_identity_when_not_defensive(self) -> None:
        resource = self._resource(defensive=False)
        stored = resource.models[0]
        assert resource.clone_for_output(stored) is stored


# ---------------------------------------------------------------------------
# Cache strategy (inherited) + query surface gate
# ---------------------------------------------------------------------------


class TestCacheAndQuerySurface:
    def test_read_only_gets_the_optimistic_private_strategy(self) -> None:
        strategy = _resource().get_cache_strategy()
        assert isinstance(strategy, OptimisticCacheStrategy)
        assert strategy.private is True
        assert strategy.expire_in == 600

    def test_queryable_fields_are_the_read_model_fields(self) -> None:
        assert _resource().get_queryable_fields() == frozenset({"id", "name", "iso3", "population"})

    def test_projected_away_field_is_not_queryable(self) -> None:
        class Guarded(BaseModel):
            id: str
            name: str
            secret: Annotated[str, DtoField(in_read_response=False, in_search_response=False)]

        resource = ListResource(
            [Guarded(id="a", name="a", secret="hush")],
            path="guardeds",
            encryption_service=_encryption(),
        )
        assert "secret" not in resource.get_queryable_fields()
        assert "secret" not in resource.get_sortable_fields()

    def test_filter_operators_follow_the_field_type(self) -> None:
        operators = _resource().get_filter_operators()
        assert operators["name"] == frozenset({"eq", "gt", "ge", "lt", "le", "contains"})
        assert operators["population"] == frozenset({"eq", "gt", "ge", "lt", "le"})


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class TestListHttp:
    @pytest_asyncio.fixture
    async def client(self) -> Any:
        manifest = Manifest(resources=(_resource(),))
        async with manifest, _client(manifest) as client:
            yield client

    async def test_search_and_read(self, client: AsyncClient) -> None:
        listing = await client.get("/countries")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == ["ca", "mx", "us"]
        one = await client.get("/countries/ca")
        assert one.status_code == 200
        assert one.json()["name"] == "Canada"

    async def test_read_missing_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/countries/zz")).status_code == 404

    async def test_count(self, client: AsyncClient) -> None:
        assert (await client.get("/countries/count")).json() == 3

    async def test_filter_and_sort_params(self, client: AsyncClient) -> None:
        filtered = await client.get("/countries?name__contains=an")
        assert [item["id"] for item in filtered.json()["items"]] == ["ca"]
        sorted_ = await client.get("/countries?sort=population&desc=true")
        assert [item["id"] for item in sorted_.json()["items"]] == ["us", "mx", "ca"]

    async def test_unknown_filter_is_400(self, client: AsyncClient) -> None:
        assert (await client.get("/countries?secret__eq=x")).status_code == 400

    async def test_unknown_sort_is_400(self, client: AsyncClient) -> None:
        assert (await client.get("/countries?sort=secret")).status_code == 400

    async def test_batch_read(self, client: AsyncClient) -> None:
        body = (await client.get("/countries/batch-read?id=ca&id=zz")).json()
        assert [item["id"] if item else None for item in body] == ["ca", None]

    async def test_cursor_paging_over_http(self, client: AsyncClient) -> None:
        first = (await client.get("/countries?limit=1")).json()
        second = (await client.get("/countries?limit=1&cursor=" + first["next_cursor"])).json()
        assert first["items"][0]["id"] == "ca"
        assert second["items"][0]["id"] == "mx"

    async def test_cursor_sort_mismatch_is_400(self, client: AsyncClient) -> None:
        first = (await client.get("/countries?sort=population&limit=1")).json()
        response = await client.get(
            "/countries?sort=population&desc=true&cursor=" + first["next_cursor"]
        )
        assert response.status_code == 400

    async def test_no_write_route_is_mounted(self, client: AsyncClient) -> None:
        assert (
            await client.post("/countries", json={"id": "fr", "name": "France"})
        ).status_code == 405
        assert (await client.patch("/countries/us", json={"name": "X"})).status_code == 405
        assert (await client.delete("/countries/us")).status_code == 405
        assert (await client.post("/countries/batch-edit", json=[])).status_code == 405

    async def test_cache_headers_are_private_with_a_window(self, client: AsyncClient) -> None:
        response = await client.get("/countries")
        assert "private" in response.headers["cache-control"]
        assert "max-age" in response.headers["cache-control"]

    async def test_projected_away_field_is_hidden_on_every_route(self) -> None:
        class Guarded(BaseModel):
            id: str
            name: str
            secret: Annotated[str, DtoField(in_read_response=False, in_search_response=False)]

        resource = ListResource(
            [Guarded(id="a", name="A", secret="hush")],
            path="guardeds",
            encryption_service=_encryption(),
        )
        manifest = Manifest(resources=(resource,))
        async with manifest, _client(manifest) as client:
            listing = (await client.get("/guardeds")).json()
            assert "secret" not in listing["items"][0]
            assert "secret" not in (await client.get("/guardeds/a")).json()
            batch = (await client.get("/guardeds/batch-read?id=a")).json()
            assert "secret" not in batch[0]


# ---------------------------------------------------------------------------
# Registration / lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_double_entry_raises(self) -> None:
        resource = _resource()
        async with resource:
            with pytest.raises(ServiceError):
                await resource.__aenter__()

    async def test_manifest_registers_the_resource(self) -> None:
        resource = _resource()
        manifest = Manifest(resources=(resource,))
        assert resource.get_manifest() is manifest
        assert manifest.get_resource("countries") is resource
