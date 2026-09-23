"""End-to-end test: two DTO-derived resources serve the standard REST surface.

``v2/core`` deliberately excludes HTTP concerns, so this test wires the derived
REST models and the resource's ``get_service_dependency`` onto a FastAPI app
itself — the minimal transport a consumer would write — and drives it with
HTTP. It pins the acceptance criterion that a Manifest of two DTO-derived
resources serves the standard surface with the models derived from the DTO.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.v2.core.dto import DTO
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import SqlResource
from resourcey.v2.core.service import Action, NotFoundError, Service


class Thread(DTO):
    id: int
    title: str


class Message(DTO):
    id: int
    thread_id: int
    body: str


def _project(instance: Any, model: type[Any]) -> dict[str, Any]:
    """Project a DTO instance onto a derived REST model (dropping MISSING fields)."""
    from resourcey.v2.core.dto import MISSING

    payload = {name: value for name, value in instance.__dict__.items() if value is not MISSING}
    return model.model_validate(payload).model_dump(mode="json")


def build_app(manifest: Manifest[Any]) -> FastAPI:
    """Mount the standard action routes for each resource onto a fresh FastAPI app."""
    app = FastAPI()
    for resource in manifest.resources:
        _mount_resource(app, resource)

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def _id_type(models: Any, id_field: str) -> type:
    """The declared type of the identifier, taken from the read model."""
    annotation = models.read_response.model_fields[id_field].annotation
    return annotation if isinstance(annotation, type) else str


def _mount_resource(app: FastAPI, resource: Any) -> None:
    """Register one resource's supported action routes (closures bound per resource)."""
    exposed = resource.get_exposed_resource()
    assert exposed is not None
    path = f"/{exposed.get_resource_path()}"
    models = exposed.get_rest_models()
    supported = exposed.get_supported_actions()
    dep = Depends(exposed.get_service_dependency)
    id_field = exposed.get_id_field()
    id_type = _id_type(models, id_field)
    prefix = exposed.get_resource_path()

    if Action.CREATE in supported:

        async def create(
            payload: Any, service: Service[Any] = dep, _models: Any = models, _prefix: str = prefix
        ) -> Any:
            created = await service.create(payload)
            return _project(created, _models.create_response)

        create.__annotations__["payload"] = models.create_request
        app.post(path, status_code=201)(create)

    if Action.SEARCH in supported:

        async def search(
            service: Service[Any] = dep,
            _models: Any = models,
            _prefix: str = prefix,
        ) -> Any:
            page = await service.search(limit=20)
            return {
                "items": [_project(i, _models.search_response) for i in page.items],
                "limit": page.limit,
                "next_cursor": page.next_cursor,
            }

        app.get(path)(search)

    if Action.COUNT in supported:

        async def count(
            service: Service[Any] = dep,
            _models: Any = models,
            _prefix: str = prefix,
        ) -> int:
            return await service.count()

        app.get(f"{path}/count")(count)

    if Action.READ in supported:

        async def read(
            id: id_type,  # noqa: A002
            service: Service[Any] = dep,
            _models: Any = models,
            _prefix: str = prefix,
        ) -> Any:
            found = await service.read(id)
            return _project(found, _models.read_response)

        read.__annotations__["id"] = id_type
        app.get(f"{path}/{{id}}")(read)

    if Action.UPDATE in supported:

        async def update(
            id: id_type,  # noqa: A002
            payload: Any,
            service: Service[Any] = dep,
            _models: Any = models,
            _prefix: str = prefix,
        ) -> Any:
            updated = await service.update(id, payload)
            return _project(updated, _models.update_response)

        update.__annotations__["id"] = id_type
        update.__annotations__["payload"] = models.update_request
        app.patch(f"{path}/{{id}}")(update)

    if Action.DELETE in supported:

        async def delete(
            id: id_type,  # noqa: A002
            service: Service[Any] = dep,
            _models: Any = models,
            _prefix: str = prefix,
        ) -> None:
            await service.delete(id)

        delete.__annotations__["id"] = id_type
        delete.__annotations__["return"] = None
        app.delete(f"{path}/{{id}}", status_code=204)(delete)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(threads.metadata.create_all)
        await conn.run_sync(messages.metadata.create_all)

    manifest: Manifest[Any] = Manifest(resources=[threads, messages])
    app = build_app(manifest)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
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


async def test_second_resource_serves_its_own_derived_models(client: AsyncClient):
    thread = (await client.post("/threads", json={"title": "t"})).json()
    message = await client.post("/messages", json={"thread_id": thread["id"], "body": "hi"})
    assert message.status_code == 201
    assert message.json() == {"id": message.json()["id"], "thread_id": thread["id"], "body": "hi"}

    # The two resources produce distinct derived model classes and paths.
    assert (await client.get("/messages")).status_code == 200


async def test_read_response_uses_the_read_model_projections(client: AsyncClient):
    created = (await client.post("/threads", json={"title": "x"})).json()
    read = (await client.get(f"/threads/{created['id']}")).json()
    # The read model for Thread is id + title (both default-visible).
    assert set(read) == {"id", "title"}


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

    manifest: Manifest[Any] = Manifest(resources=[countries])
    app = build_app(manifest)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
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
