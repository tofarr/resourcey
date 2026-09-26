"""Tests for ``ResourceView`` (issue #121).

The view is the configured, narrowing wrapper over another ``v2`` resource. The
tests cover, by layer:

* construction validation - unknown field override, identifier override, widening
  actions, and field hiding against a declared object filter;
* field projection - the six REST models, the query / sort surface, and the
  cache validator all narrow together;
* action overrides - routes, batch propagation, and the normalization of a
  batch action without its singular action;
* the service seam - ``ViewService`` re-asserts the view's actions for a direct
  caller, and the lifecycle delegates to the inner;
* the end-to-end secret use case over HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.cache.cache_strategy import ETagCacheStrategy, OptimisticCacheStrategy
from resourcey.v2.core.dto import DTO, DtoField, derive_dto
from resourcey.v2.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.service import (
    Action,
    Create,
    Delete,
    ServiceError,
    Update,
    normalize_actions,
)
from resourcey.v2.http.app import create_app
from resourcey.v2.mongo.embedded import AsyncEmbeddedClient
from resourcey.v2.mongo.mongo_resource import MongoResource
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.util.search_filter import BaseObjectFilter
from resourcey.v2.util.sort_order import AttrSortOrder
from resourcey.v2.view.resource_view import ResourceView
from resourcey.v2.view.view_service import ViewService


class ViewBase(DeclarativeBase):
    pass


class Secret(ViewBase):
    __tablename__ = "view_secrets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    description: Mapped[str | None] = mapped_column(String(100), nullable=True)
    value: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


SECRET_HIDING = {
    "value": {
        "in_read_response": False,
        "in_search_response": False,
        "in_update_request": False,
        "in_update_response": False,
    }
}


def _maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_async_engine("sqlite+aiosqlite:///:memory:"), expire_on_commit=False
    )


async def _inner(engine: Any, maker: async_sessionmaker[AsyncSession]) -> SqlResource[Any, Any]:
    resource: SqlResource[Any, Any] = SqlResource(Secret, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(ViewBase.metadata.create_all)
    return resource


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_unknown_field_override_is_rejected(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        with pytest.raises(ResourceyConfigError, match="unknown field"):
            ResourceView(inner, exposed_field_overrides={"nope": {"in_read_response": False}})

    def test_identifier_override_is_rejected(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        with pytest.raises(ResourceyConfigError, match="identifier"):
            ResourceView(inner, exposed_field_overrides={"id": {"in_read_response": False}})

    def test_rewidening_an_inner_hidden_field_is_rejected(self) -> None:
        # A view whose own DTO already hides ``value`` from the read model.
        inner = SqlResource(Secret, session_factory=_maker())
        inner_view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        # A second view may not turn that flag back on.
        with pytest.raises(ResourceyConfigError, match="cannot re-widen"):
            ResourceView(inner_view, exposed_field_overrides={"value": {"in_read_response": True}})
        # Narrowing further is fine.
        ResourceView(
            inner_view, exposed_field_overrides={"description": {"in_read_response": False}}
        )

    def test_widening_actions_is_rejected(self) -> None:
        class ReadOnly(SqlResource[Any, Any]):
            def get_supported_actions(self) -> frozenset[Action]:
                return frozenset({Action.READ})

        readonly = ReadOnly(Secret, session_factory=_maker())
        with pytest.raises(ResourceyConfigError, match="cannot expose"):
            ResourceView(readonly, exposed_actions=frozenset({Action.READ, Action.CREATE}))
        # A subset is fine.
        ResourceView(readonly, exposed_actions=frozenset({Action.READ}))

    def test_hiding_fields_against_a_declared_filter_is_rejected(self) -> None:
        class SecretFilter(BaseObjectFilter[Any]):
            value__eq: str | None = None

        class DeclaredFilter(SqlResource[Any, Any]):
            def get_search_filter_type(self) -> type[Any] | None:
                return SecretFilter

        inner = DeclaredFilter(Secret, session_factory=_maker())
        with pytest.raises(ResourceyConfigError, match="get_search_filter_type"):
            ResourceView(inner, exposed_field_overrides=SECRET_HIDING)

    def test_a_resource_without_a_dto_declaration_cannot_be_viewed(self) -> None:
        class Plain:
            def get_supported_actions(self) -> frozenset[Action]:
                return frozenset({Action.READ})

        with pytest.raises(ResourceyConfigError, match="get_dto_declaration"):
            ResourceView(Plain())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


class TestFieldProjection:
    def test_the_six_rest_models_are_the_views(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        models = view.get_rest_models()
        # ``value`` keeps its create request + create response (one-time reveal)
        # and is gone from every other shape.
        assert "value" in models.create_request.model_fields
        assert "value" in models.create_response.model_fields
        assert "value" not in models.update_request.model_fields
        assert "value" not in models.update_response.model_fields
        assert "value" not in models.read_response.model_fields
        assert "value" not in models.search_response.model_fields
        # The inner is untouched.
        assert "value" in inner.get_rest_models().read_response.model_fields

    def test_query_and_sort_surface_narrow_with_the_read_model(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        assert "value" not in view.get_queryable_fields()
        assert "value" not in view.get_sortable_fields()
        assert "value" not in view.get_filter_operators()
        with pytest.raises(InvalidInputError):
            view.resolve_sort_order("value", False)

    def test_override_merges_onto_the_inner_field(self) -> None:
        # The inner hides ``description`` from search; the view hides ``value``
        # from read. A full-DtoField replacement would have re-widened
        # ``description``'s search flag - the merge must not.
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        fields = view.get_dto_declaration().get_fields()
        assert fields["value"].in_read_response is False
        # Unmentioned flags keep the inner's value (``value`` is still creatable).
        assert fields["value"].in_create_request is True

    def test_a_pass_through_view_keeps_the_inner_models(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner)
        assert view.get_rest_models() is inner.get_rest_models()
        assert view.get_dto_declaration() is inner.get_dto_declaration()


# ---------------------------------------------------------------------------
# DTO-first resources (``Annotated[T, DtoField(...)]`` declarations)
#
# The SQL backend passes a ``DtoField`` as a *class attribute*; the DTO-first
# backends (Mongo / list) declare it *inside* ``Annotated``. Both must project
# identically: the ``Annotated`` form stores the inner ``DtoField`` in the
# annotation, and Pydantic flattens a nested ``Annotated``, so ``derive_dto``
# must reduce to the base type or the override is silently ignored.
# ---------------------------------------------------------------------------


class AnnotatedDTO(DTO, metadata={"collection": "annotated"}):
    id: Annotated[
        UUID,
        DtoField(
            in_create_request=False, in_update_request=False, default_factory_for_create=uuid4
        ),
    ]
    description: Annotated[str | None, DtoField(default_for_create=None)]
    value: Annotated[str, DtoField()]


class TestAnnotatedDeclarations:
    def test_derive_dto_applies_overrides_on_the_annotated_form(self) -> None:
        derived = derive_dto(
            AnnotatedDTO,
            field_overrides={"value": {"in_read_response": False, "in_search_response": False}},
        )
        fields = derived.get_fields()
        assert fields["value"].in_read_response is False
        assert fields["value"].in_search_response is False
        assert "value" not in derived.get_rest_models().read_response.model_fields
        assert "value" not in derived.get_rest_models().search_response.model_fields
        # Unmentioned flags keep the inner's value (the merge, not a replacement).
        assert fields["value"].in_create_request is True

    def test_derive_dto_keeps_unmentioned_annotated_fields_intact(self) -> None:
        derived = derive_dto(
            AnnotatedDTO,
            field_overrides={"description": {"in_read_response": False}},
        )
        # The convention-generated ``uuid4`` factory survives (conventions do
        # not re-run) and the untouched optional field keeps its annotation.
        assert derived.get_fields()["id"].default_factory_for_create is uuid4
        assert "description" not in derived.get_rest_models().read_response.model_fields
        assert derived.get_fields()["value"].in_read_response is True

    def test_derive_dto_carries_class_metadata(self) -> None:
        derived = derive_dto(AnnotatedDTO, field_overrides={"value": {"in_read_response": False}})
        assert derived.metadata == {"collection": "annotated"}

    def test_an_annotated_view_narrows_the_query_surface(self) -> None:
        inner = MongoResource(AnnotatedDTO, client=AsyncEmbeddedClient(), path="annotated")
        view = ResourceView(
            inner,
            exposed_field_overrides={
                "value": {"in_read_response": False, "in_search_response": False}
            },
        )
        assert "value" not in view.get_rest_models().read_response.model_fields
        assert "value" not in view.get_queryable_fields()
        assert "value" not in view.get_sortable_fields()
        assert "value" not in view.get_filter_operators()
        with pytest.raises(InvalidInputError):
            view.resolve_sort_order("value", False)
        # A still-exposed field sorts normally.
        order = view.resolve_sort_order("description", True)
        assert order == AttrSortOrder(attribute="description", descending=True)


# ---------------------------------------------------------------------------
# Actions + batch propagation
# ---------------------------------------------------------------------------


class TestActions:
    def test_actions_default_to_the_inner(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner)
        assert view.get_supported_actions() == normalize_actions(frozenset(Action))

    def test_removing_update_normalizes_batch_edit_kind(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_actions=frozenset(Action) - {Action.UPDATE})
        actions = view.get_supported_actions()
        assert Action.UPDATE not in actions
        assert Action.BATCH_EDIT in actions  # create / delete still batch

    def test_removing_read_drops_batch_read(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_actions=frozenset(Action) - {Action.READ})
        actions = view.get_supported_actions()
        assert Action.READ not in actions
        assert Action.BATCH_READ not in actions  # normalized away

    def test_removing_all_write_actions_drops_batch_edit(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        read_only = frozenset(
            {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ, Action.BATCH_EDIT}
        )
        view = ResourceView(inner, exposed_actions=read_only)
        actions = view.get_supported_actions()
        assert Action.BATCH_EDIT not in actions
        assert Action.BATCH_READ in actions


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_a_read_only_view_is_optimistic_and_private(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        read_only = frozenset({Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ})
        view = ResourceView(inner, exposed_actions=read_only)
        strategy = view.get_cache_strategy()
        assert isinstance(strategy, OptimisticCacheStrategy)
        assert strategy.private is True

    def test_a_writable_view_recomputes_an_etag(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, exposed_actions=frozenset(Action) - {Action.UPDATE})
        assert isinstance(view.get_cache_strategy(), ETagCacheStrategy)

    def test_a_pass_through_view_delegates_the_strategy(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner)
        assert view.get_cache_strategy() is inner.get_cache_strategy()

    def test_an_explicit_strategy_wins(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        explicit = OptimisticCacheStrategy(expire_in=1, private=True)
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING, cache_strategy=explicit)
        assert view.get_cache_strategy() is explicit


# ---------------------------------------------------------------------------
# Service seam + lifecycle
# ---------------------------------------------------------------------------


class TestServiceSeam:
    async def test_view_service_forwards_and_enforces_actions(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner = await _inner(engine, maker)
        view = ResourceView(
            inner,
            exposed_field_overrides=SECRET_HIDING,
            exposed_actions=frozenset(Action) - {Action.UPDATE},
        )
        async with await view.get_service() as service:
            assert isinstance(service, ViewService)
            created = await service.create(
                view.get_dto_type().model_validate({"description": "d", "value": "v"})
            )
            read = await service.read(created.id)
            assert read.value == "v"
            # A hidden batch kind is refused even though the inner supports it.
            with pytest.raises(InvalidInputError, match="update is not exposed"):
                await service.batch_edit(
                    [
                        Update(
                            item=view.get_dto_type().model_validate(
                                {"id": created.id, "description": "x"}
                            )
                        )
                    ]
                )
        await engine.dispose()

    async def test_lifecycle_delegates_to_the_inner(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        await _inner(engine, maker)
        entered: list[str] = []

        class Recording(SqlResource[Any, Any]):
            async def __aenter__(self) -> Any:
                entered.append("enter")
                return await super().__aenter__()

            async def __aexit__(self, *exc: object) -> None:
                entered.append("exit")
                await super().__aexit__(*exc)

        recording = Recording(Secret, session_factory=maker)
        view = ResourceView(recording)
        async with view:
            assert entered == ["enter"]
        assert entered == ["enter", "exit"]
        await engine.dispose()

    def test_registration_forwards_to_the_inner(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner)
        manifest = Manifest(resources=[view])
        assert view.get_manifest() is manifest
        assert inner.get_manifest() is manifest

    async def test_every_standard_action_forwards(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner = await _inner(engine, maker)
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        async with await view.get_service() as service:
            dto_type = view.get_dto_type()
            first = await service.create(
                dto_type.model_validate({"description": "a", "value": "1"})
            )
            second = await service.create(
                dto_type.model_validate({"description": "b", "value": "2"})
            )
            assert (await service.read(first.id)).description == "a"
            assert await service.count() == 2
            page = await service.search(limit=10)
            assert {item.description for item in page.items} == {"a", "b"}
            assert [
                item.description for item in await service.batch_read([first.id, second.id])
            ] == [
                "a",
                "b",
            ]
            created = await service.batch_edit(
                [Create(item=dto_type.model_validate({"description": "c", "value": "3"}))]
            )
            assert len(created) == 1
            await service.delete(second.id)
            assert await service.count() == 2
            updated = await service.update(
                dto_type.model_validate({"id": first.id, "description": "z"})
            )
            assert updated.description == "z"
        await engine.dispose()

    async def test_batch_edit_refuses_create_and_delete_when_hidden(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner = await _inner(engine, maker)
        read_only = frozenset({Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ})
        view = ResourceView(inner, exposed_field_overrides=SECRET_HIDING, exposed_actions=read_only)
        async with await view.get_service() as service:
            dto_type = view.get_dto_type()
            with pytest.raises(InvalidInputError, match="create is not exposed"):
                await service.batch_edit(
                    [Create(item=dto_type.model_validate({"description": "d", "value": "v"}))]
                )
            with pytest.raises(InvalidInputError, match="delete is not exposed"):
                await service.batch_edit([Delete(id=uuid4())])
        await engine.dispose()

    async def test_a_service_used_before_entering_raises(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner = await _inner(engine, maker)
        view = ResourceView(inner)
        service = await view.get_service()
        with pytest.raises(ServiceError, match="before entering it"):
            await service.count()
        await engine.dispose()

    async def test_double_entry_raises(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        inner = await _inner(engine, maker)
        view = ResourceView(inner)
        async with view:
            with pytest.raises(ServiceError, match="already entered"):
                await view.__aenter__()
        await engine.dispose()

    def test_an_explicit_path_wins(self) -> None:
        inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=_maker())
        view = ResourceView(inner, path="/public-secrets")
        assert view.get_resource_path() == "public-secrets"

    def test_a_declared_sort_order_type_is_carried_across(self) -> None:
        class Declared(SqlResource[Any, Any]):
            def get_sort_order_type(self) -> type[Any] | None:
                return AttrSortOrder

        inner = Declared(Secret, session_factory=_maker())
        view = ResourceView(inner)
        assert view.resolve_sort_order("description", True) == AttrSortOrder(
            attribute="description", descending=True
        )
        hidden = ResourceView(inner, exposed_field_overrides=SECRET_HIDING)
        with pytest.raises(InvalidInputError):
            hidden.resolve_sort_order("value", False)
        assert hidden.resolve_sort_order(None, False) is None

    def test_a_declared_filter_is_carried_across_when_not_narrowing(self) -> None:
        class SecretFilter(BaseObjectFilter[Any]):
            value__eq: str | None = None

        class Declared(SqlResource[Any, Any]):
            def get_search_filter_type(self) -> type[Any] | None:
                return SecretFilter

        inner = Declared(Secret, session_factory=_maker())
        view = ResourceView(inner)  # no field overrides -> not narrowing
        assert view.get_search_filter_type() is SecretFilter


# ---------------------------------------------------------------------------
# End-to-end: the secret use case
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def secret_client() -> AsyncIterator[AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    inner: SqlResource[Any, Any] = SqlResource(Secret, session_factory=maker)
    view = ResourceView(
        inner,
        exposed_field_overrides=SECRET_HIDING,
        exposed_actions=frozenset(Action) - {Action.UPDATE},
    )
    async with engine.begin() as conn:
        await conn.run_sync(ViewBase.metadata.create_all)
    manifest = Manifest(resources=[view])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    await engine.dispose()


class TestSecretEndToEnd:
    async def test_one_time_reveal(self, secret_client: AsyncClient) -> None:
        created = await secret_client.post("/secrets", json={"description": "d", "value": "s3cr3t"})
        assert created.status_code == 201
        body = created.json()
        assert body["value"] == "s3cr3t"  # revealed once, on create
        sid = body["id"]

        read = await secret_client.get(f"/secrets/{sid}")
        assert read.status_code == 200
        assert "value" not in read.json()

        listed = await secret_client.get("/secrets")
        assert listed.status_code == 200
        assert all("value" not in item for item in listed.json()["items"])

    async def test_update_is_not_exposed(self, secret_client: AsyncClient) -> None:
        created = (
            await secret_client.post("/secrets", json={"description": "d", "value": "s"})
        ).json()
        assert (
            await secret_client.patch(f"/secrets/{created['id']}", json={"description": "x"})
        ).status_code == 405
        batch = await secret_client.post(
            "/secrets/batch-edit",
            json=[{"kind": "Update", "item": {"id": created["id"], "description": "x"}}],
        )
        assert batch.status_code == 422

    async def test_hidden_field_is_not_queryable(self, secret_client: AsyncClient) -> None:
        await secret_client.post("/secrets", json={"description": "d", "value": "s"})
        assert (await secret_client.get("/secrets", params={"value__eq": "s"})).status_code == 400
        assert (await secret_client.get("/secrets", params={"sort": "value"})).status_code == 400

    async def test_the_secret_still_reaches_storage(self, secret_client: AsyncClient) -> None:
        # The create request still carries ``value``, so the write lands even
        # though no response after create discloses it.
        created = (
            await secret_client.post("/secrets", json={"description": "d", "value": "hidden"})
        ).json()
        assert (await secret_client.get("/secrets/count")).json() == 1
        assert "value" not in (await secret_client.get(f"/secrets/{created['id']}")).json()
