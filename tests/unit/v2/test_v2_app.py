"""End-to-end tests for the ``v2`` HTTP transport (issue #87).

``v2/core`` deliberately excludes HTTP concerns, so this test drives the real
:func:`~resourcey.v2.http.app.create_app` /
:func:`~resourcey.v2.http.app.add_to_app` free functions — which wire a manifest
of DTO-derived resources onto FastAPI: per-action routes with the derived REST
models, the error envelope, and optional CORS. The manifest is
:func:`create_app`'s lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.v2.core.dto import DTO, DtoField
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Action, NotFoundError, ServiceError
from resourcey.v2.http.app import add_to_app, create_app
from resourcey.v2.http.routes import register_error_handlers, register_routes
from resourcey.v2.sql.resource import SqlResource


class Thread(DTO):
    id: int
    title: str


class Message(DTO):
    id: int
    thread_id: int
    body: str


class StoredKey(DTO):
    """A one-time-reveal DTO: ``secret`` is in the create response and nowhere else."""

    id: int
    name: str
    secret: str = DtoField(
        in_read_response=False,
        in_update_response=False,
        in_search_response=False,
        in_update_request=False,
    )


class Hidden(DTO):
    id: int
    value: str


class HiddenResource(Resource[Any]):
    """A resource the outside world never sees (``get_exposed_resource() is None``)."""

    def get_exposed_resource(self) -> None:
        return None


async def _make_client(manifest: Manifest, app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)
        await conn.run_sync(messages.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads, messages])
    async for c in _make_client(manifest, create_app(manifest)):
        yield c
    await engine.dispose()


async def test_create_read_update_delete_over_http(client: AsyncClient):
    created = await client.post("/threads", json={"title": "hello"})
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "hello"
    thread_id = body["id"]

    fetched = await client.get(f"/threads/{thread_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "hello"

    updated = await client.patch(f"/threads/{thread_id}", json={"title": "bye"})
    assert updated.status_code == 200
    assert updated.json()["title"] == "bye"

    deleted = await client.delete(f"/threads/{thread_id}")
    assert deleted.status_code == 204

    missing = await client.get(f"/threads/{thread_id}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_search_and_count_over_http(client: AsyncClient):
    await client.post("/threads", json={"title": "a"})
    await client.post("/threads", json={"title": "b"})

    counted = await client.get("/threads/count")
    assert counted.status_code == 200
    assert counted.json() == 2

    page = await client.get("/threads")
    assert page.status_code == 200
    titles = {item["title"] for item in page.json()["items"]}
    assert titles == {"a", "b"}


async def test_batch_read_and_batch_edit_over_http(client: AsyncClient):
    created = (await client.post("/threads", json={"title": "t"})).json()

    read = await client.get("/threads/batch-read", params={"id": [created["id"], 999]})
    assert read.status_code == 200
    assert read.json() == [{"id": created["id"], "title": "t"}, None]

    edited = await client.post(
        "/threads/batch-edit",
        json=[{"id": created["id"], "title": "edited"}, {"id": 999, "title": "nope"}],
    )
    assert edited.status_code == 200
    assert edited.json() == [{"id": created["id"], "title": "edited"}, None]


async def test_second_resource_serves_its_own_derived_models(client: AsyncClient):
    thread = (await client.post("/threads", json={"title": "t"})).json()
    message = await client.post("/messages", json={"thread_id": thread["id"], "body": "hi"})
    assert message.status_code == 201
    assert message.json() == {
        "id": message.json()["id"],
        "thread_id": thread["id"],
        "body": "hi",
    }

    assert (await client.get("/messages")).status_code == 200


async def test_read_response_uses_the_read_model_projections(client: AsyncClient):
    created = (await client.post("/threads", json={"title": "x"})).json()
    read = (await client.get(f"/threads/{created['id']}")).json()
    assert set(read) == {"id", "title"}


async def test_create_response_carries_a_one_time_reveal_field():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    keys = SqlResource(StoredKey, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(keys.metadata.create_all)

    manifest: Manifest = Manifest(resources=[keys])
    async for client in _make_client(manifest, create_app(manifest)):
        created = await client.post("/stored-keys", json={"name": "k", "secret": "s3cr3t"})
        assert created.status_code == 201
        # The create response reveals the secret...
        assert created.json() == {"id": created.json()["id"], "name": "k", "secret": "s3cr3t"}
        # ...but read/search never do (the field is not in those read models).
        read = await client.get(f"/stored-keys/{created.json()['id']}")
        assert read.json() == {"id": created.json()["id"], "name": "k"}
        page = await client.get("/stored-keys")
        assert page.json()["items"] == [{"id": created.json()["id"], "name": "k"}]
    await engine.dispose()


async def test_hidden_resource_mounts_no_routes():
    hidden = HiddenResource(Hidden)
    manifest: Manifest = Manifest(resources=[hidden])
    async for client in _make_client(manifest, create_app(manifest)):
        assert (await client.get("/hiddens")).status_code == 404
        assert (await client.post("/hiddens", json={"value": "v"})).status_code == 404


class Country(DTO, id_field_name="code"):
    code: str
    name: str


@pytest_asyncio.fixture
async def code_client() -> AsyncIterator[AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    countries = SqlResource(Country, session_factory=maker, path="/countries")
    async with engine.begin() as conn:
        await conn.run_sync(countries.metadata.create_all)

    manifest: Manifest = Manifest(resources=[countries])
    async for c in _make_client(manifest, create_app(manifest)):
        yield c
    await engine.dispose()


async def test_custom_identifier_over_http(code_client: AsyncClient):
    # The identifier is client-supplied (not a create-request field, no autoincrement).
    created = await code_client.post("/countries", json={"code": "US", "name": "United States"})
    assert created.status_code == 201
    assert created.json() == {"code": "US", "name": "United States"}

    fetched = await code_client.get("/countries/US")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "United States"

    updated = await code_client.patch("/countries/US", json={"name": "USA"})
    assert updated.status_code == 200
    assert updated.json() == {"code": "US", "name": "USA"}

    assert (await code_client.delete("/countries/US")).status_code == 204
    assert (await code_client.get("/countries/US")).status_code == 404


async def test_add_to_app_mounts_prefix_without_wiring_lifespan():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    app = FastAPI()
    add_to_app(manifest, app, prefix="/api/v1")
    # add_to_app must not install the manifest as the app's lifespan: running the
    # app's lifespan leaves the manifest un-entered.
    async with app.router.lifespan_context(app):
        assert not manifest.entered

    async with (
        manifest,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.post("/api/v1/threads", json={"title": "t"})).status_code == 201
        assert (await client.get("/threads")).status_code == 404
    await engine.dispose()


def _route_pairs(app_or_router: Any) -> set[tuple[str, str]]:
    return {
        (getattr(route, "path", ""), next(iter(getattr(route, "methods", set()))))
        for route in app_or_router.routes
        if getattr(route, "methods", None)
    }


async def test_register_routes_registers_every_action_and_tags_the_router():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)

    app = FastAPI()
    router = register_routes(app, threads)
    paths = _route_pairs(router)
    assert ("/threads", "POST") in paths
    assert ("/threads", "GET") in paths
    assert ("/threads/count", "GET") in paths
    assert ("/threads/batch-read", "GET") in paths
    assert ("/threads/batch-edit", "POST") in paths
    assert ("/threads/{id}", "GET") in paths
    assert ("/threads/{id}", "PATCH") in paths
    assert ("/threads/{id}", "DELETE") in paths
    # The tag is the *exposed* resource's class name (here the SqlResource).
    assert router.tags == ["SqlResource"]

    await engine.dispose()


async def test_register_routes_explicit_prefix_and_tags():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)

    app = FastAPI()
    router = register_routes(app, threads, prefix="/x", tags=["custom"])
    assert router.tags == ["custom"]
    await engine.dispose()


async def test_register_routes_accepts_an_api_router():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)

    target = APIRouter()
    register_routes(target, threads)
    app = FastAPI()
    app.include_router(target)
    register_error_handlers(app)

    manifest: Manifest = Manifest(resources=[threads])
    async with (
        manifest,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.post("/threads", json={"title": "t"})).status_code == 201
    await engine.dispose()


async def test_register_routes_hidden_resource_returns_an_empty_router():
    hidden = HiddenResource(Hidden)
    app = FastAPI()
    router = register_routes(app, hidden)
    assert router.routes == []
    assert router.tags == ["HiddenResource"]


async def test_register_routes_narrows_to_supported_actions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    class ReadOnlyThread(SqlResource[Any]):
        def get_supported_actions(self) -> frozenset[Action]:
            return frozenset({Action.READ, Action.SEARCH, Action.COUNT})

    resource = ReadOnlyThread(Thread, session_factory=maker)
    app = FastAPI()
    router = register_routes(app, resource)
    paths = _route_pairs(router)
    assert ("/threads/{id}", "GET") in paths
    assert ("/threads", "GET") in paths
    assert ("/threads/count", "GET") in paths
    assert ("/threads/{id}", "PATCH") not in paths
    assert ("/threads", "POST") not in paths
    await engine.dispose()


async def test_route_escape_hatch_preserves_a_developer_route():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    app = FastAPI()
    custom = APIRouter()

    @custom.get("/threads")
    async def custom_search() -> dict[str, bool]:
        return {"custom": True}

    app.include_router(custom)
    add_to_app(manifest, app)

    async with (
        manifest,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/threads")
        assert response.json() == {"custom": True}
    await engine.dispose()


async def test_search_ignores_unknown_query_params():
    """There is no sort / filter surface yet (#79), so extra params are ignored."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    async for client in _make_client(manifest, create_app(manifest)):
        await client.post("/threads", json={"title": "a"})
        page = await client.get("/threads", params={"limit": 1, "bogus": "x"})
        assert page.status_code == 200
        assert len(page.json()["items"]) == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def _error_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError(42)

    @app.get("/service-error")
    async def _service_error() -> None:
        raise ServiceError("boom")

    @app.get("/conflict")
    async def _conflict() -> None:
        raise IntegrityError("stmt", {}, Exception("duplicate"))

    @app.get("/validate")
    async def _validate(count: int) -> dict[str, int]:
        return {"count": count}

    return app


@pytest_asyncio.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=_error_app()), base_url="http://test"
    ) as client:
        yield client


async def test_not_found_error_envelope(error_client: AsyncClient):
    response = await error_client.get("/not-found")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_service_error_envelope(error_client: AsyncClient):
    response = await error_client.get("/service-error")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "internal_error", "message": "boom"}


async def test_integrity_error_envelope(error_client: AsyncClient):
    response = await error_client.get("/conflict")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_pydantic_validation_keeps_422(error_client: AsyncClient):
    response = await error_client.get("/validate", params={"count": "not-an-int"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def _cors_middleware_present(app: FastAPI) -> bool:
    names = {getattr(m.cls, "__name__", "") for m in app.user_middleware}
    return "CORSMiddleware" in names


def test_create_app_adds_cors_when_origins_configured():
    app = create_app(Manifest(resources=[]), cors_origins=["https://example.com"])
    assert _cors_middleware_present(app)


def test_create_app_no_cors_by_default():
    app = create_app(Manifest(resources=[]))
    assert not _cors_middleware_present(app)


def test_create_app_wildcard_origin_disables_credentials():
    app = create_app(Manifest(resources=[]), cors_origins=["*"])
    middleware = next(
        m for m in app.user_middleware if getattr(m.cls, "__name__", "") == "CORSMiddleware"
    )
    assert middleware.kwargs["allow_origins"] == ["*"]
    assert middleware.kwargs["allow_credentials"] is False


async def test_create_app_cors_preflight_allows_origin():
    app = create_app(Manifest(resources=[]), cors_origins=["https://example.com"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            "/threads",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers["access-control-allow-origin"] == "https://example.com"
