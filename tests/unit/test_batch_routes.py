"""The app-level ``POST /batch-edit`` spanning every exposed resource (issue #177).

Driven through the real :func:`~resourcey.http.app.create_app` so the mount
default, the static body, the action gates, ordering, and rollback are all
exercised over HTTP rather than by calling internals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, PrivateAttr
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_policy import AllowAll, PolicyResolver
from resourcey.auth.auth_principal import Authenticator
from resourcey.config.config_base import _reset_config_prefix
from resourcey.core.dto import DtoField
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.http.app import add_to_app, create_app
from resourcey.http.batch_routes import register_batch_routes
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.http_config import RequestLimitsConfig
from resourcey.list.list_resource import ListResource
from resourcey.sql.sql_resource import SqlResource
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.triggered_dependency_builder import TriggeredDependencyBuilder


class BatchBase(DeclarativeBase):
    pass


class Thread(BatchBase):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100), unique=True)


class Message(BatchBase):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(String(200))


class Tag(BatchBase):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Two SQL resources on one session factory — the shared-connection case."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads, messages])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    await engine.dispose()


# ---------------------------------------------------------------------------
# The happy path and the response contract
# ---------------------------------------------------------------------------


async def test_batch_edit_spans_resources_in_one_request(client: AsyncClient):
    thread = (await client.post("/threads", json={"title": "t"})).json()

    response = await client.post(
        "/batch-edit",
        json=[
            {
                "resource": "threads",
                "kind": "Update",
                "item": {"id": thread["id"], "title": "renamed"},
            },
            {
                "resource": "messages",
                "kind": "Create",
                "item": {"thread_id": thread["id"], "body": "hi"},
            },
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "resource": "threads",
            "kind": "Update",
            "result": {"id": thread["id"], "title": "renamed"},
        },
        {
            "resource": "messages",
            "kind": "Create",
            "result": {"id": 1, "thread_id": thread["id"], "body": "hi"},
        },
    ]
    # Both writes really landed, through the same request.
    assert (await client.get(f"/threads/{thread['id']}")).json()["title"] == "renamed"
    assert (await client.get("/messages/1")).json()["body"] == "hi"


async def test_no_cache_headers_are_emitted(client: AsyncClient):
    """No ETag / Cache-Control: the method is unsafe and resources differ per strategy."""
    thread = (await client.post("/threads", json={"title": "t"})).json()
    response = await client.post(
        "/batch-edit",
        json=[
            {"resource": "threads", "kind": "Update", "item": {"id": thread["id"], "title": "r"}},
        ],
    )
    assert response.status_code == 200
    assert "etag" not in {name.lower() for name in response.headers}
    assert "cache-control" not in {name.lower() for name in response.headers}


class Vault(BatchBase):
    """A one-time-reveal model: ``secret`` appears only in the create response."""

    __tablename__ = "vaults"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))
    secret: Mapped[str] = mapped_column(
        String(50),
        info={
            "dto_field": DtoField(
                in_read_response=False,
                in_update_response=False,
                in_search_response=False,
                in_update_request=False,
            )
        },
    )


async def test_create_projects_onto_the_create_response_and_update_onto_update():
    """A one-time-reveal field survives ``Create`` and is gone from ``Update``.

    Both kinds go through the same route, so this pins that the projection is
    chosen per node rather than per request.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    revealed = SqlResource(Vault, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[revealed])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "vaults", "kind": "Create", "item": {"name": "n", "secret": "s"}}
                ],
            )
            assert created.json()[0]["result"]["secret"] == "s"

            updated = await c.post(
                "/batch-edit",
                json=[
                    {
                        "resource": "vaults",
                        "kind": "Update",
                        "item": {"id": created.json()[0]["result"]["id"], "name": "m"},
                    }
                ],
            )
            assert updated.json()[0]["result"]["name"] == "m"
            assert "secret" not in updated.json()[0]["result"]
    await engine.dispose()


async def test_results_are_positionally_aligned_and_a_miss_is_null(client: AsyncClient):
    thread = (await client.post("/threads", json={"title": "t"})).json()

    response = await client.post(
        "/batch-edit",
        json=[
            {"resource": "threads", "kind": "Delete", "id": thread["id"]},
            {"resource": "threads", "kind": "Update", "item": {"id": 999, "title": "gone"}},
            {"resource": "messages", "kind": "Delete", "id": 999},
        ],
    )

    assert response.status_code == 200
    # A delete, an absent id, and a missing row all answer ``null`` — one entry
    # per request item, never a shortened list.
    assert [item["result"] for item in response.json()] == [None, None, None]
    assert [item["kind"] for item in response.json()] == ["Delete", "Update", "Delete"]


async def test_an_update_condition_gates_the_write(client: AsyncClient):
    thread = (await client.post("/threads", json={"title": "t"})).json()
    condition = {
        "kind": "AttrFilter",
        "attribute": "title",
        "filter": {"kind": "EqFilter", "value": "wrong"},
    }

    response = await client.post(
        "/batch-edit",
        json=[
            {
                "resource": "threads",
                "kind": "Update",
                "item": {"id": thread["id"], "title": "nope"},
                "condition": condition,
            }
        ],
    )

    assert response.status_code == 200
    assert response.json()[0]["result"] is None
    assert (await client.get(f"/threads/{thread['id']}")).json()["title"] == "t"


async def test_an_empty_batch_is_a_no_op(client: AsyncClient):
    response = await client.post("/batch-edit", json=[])
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Ordering -- the caller's order, never grouped
# ---------------------------------------------------------------------------


async def test_nodes_apply_in_the_order_given(client: AsyncClient):
    """An interleaved batch applies literally: the last write to a row wins.

    Grouping by resource would hoist the two ``threads`` edits together and give
    a different final title, so this pins strict list order.
    """
    thread = (await client.post("/threads", json={"title": "start"})).json()

    response = await client.post(
        "/batch-edit",
        json=[
            {"resource": "threads", "kind": "Update", "item": {"id": thread["id"], "title": "x"}},
            {
                "resource": "messages",
                "kind": "Create",
                "item": {"thread_id": thread["id"], "body": "m"},
            },
            {"resource": "threads", "kind": "Update", "item": {"id": thread["id"], "title": "y"}},
        ],
    )

    assert response.status_code == 200
    assert [item["resource"] for item in response.json()] == ["threads", "messages", "threads"]
    assert (await client.get(f"/threads/{thread['id']}")).json()["title"] == "y"


async def test_a_create_is_visible_to_a_later_node_in_the_same_batch():
    """Order is what makes a create-then-update of one new row expressible.

    The two nodes share a service, so the row the first created is the one the
    second renames — which only holds because they are applied in list order.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "first"}},
                    {"resource": "threads", "kind": "Create", "item": {"title": "second"}},
                    {
                        "resource": "threads",
                        "kind": "Update",
                        "item": {"id": 1, "title": "renamed"},
                    },
                ],
            )
            assert response.status_code == 200
            assert [item["result"]["title"] for item in response.json()] == [
                "first",
                "second",
                "renamed",
            ]
    await engine.dispose()


# ---------------------------------------------------------------------------
# Fail fast, best-effort rollback
# ---------------------------------------------------------------------------


async def test_the_first_failure_aborts_the_batch(client: AsyncClient):
    taken = (await client.post("/threads", json={"title": "taken"})).json()

    response = await client.post(
        "/batch-edit",
        json=[
            {"resource": "threads", "kind": "Create", "item": {"title": "ok"}},
            {"resource": "threads", "kind": "Create", "item": {"title": "taken"}},
            {"resource": "threads", "kind": "Create", "item": {"title": "never"}},
        ],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    # Nothing was applied: not the node that failed, and not the one after it.
    titles = {item["title"] for item in (await client.get("/threads")).json()["items"]}
    assert titles == {"taken"}
    assert taken["id"]


async def test_a_failed_batch_rolls_back_earlier_resources_on_one_connection(client: AsyncClient):
    """Same-connection SQL shares a session, so one failure undoes the whole batch.

    This is the best-effort guarantee the endpoint documents: it holds where the
    backend provides a shared transaction, which is the case for every SQL
    resource on one session factory.
    """
    thread = (await client.post("/threads", json={"title": "a"})).json()
    (await client.post("/threads", json={"title": "still-taken"}))
    message = (await client.post("/messages", json={"thread_id": thread["id"], "body": "m"})).json()

    response = await client.post(
        "/batch-edit",
        json=[
            {
                "resource": "threads",
                "kind": "Update",
                "item": {"id": thread["id"], "title": "renamed"},
            },
            {"resource": "messages", "kind": "Delete", "id": message["id"]},
            {"resource": "threads", "kind": "Create", "item": {"title": "still-taken"}},
        ],
    )

    assert response.status_code == 409
    # The update and the delete both came *before* the failure, and both are gone.
    assert (await client.get(f"/threads/{thread['id']}")).json()["title"] == "a"
    assert (await client.get(f"/messages/{message['id']}")).status_code == 200


# ---------------------------------------------------------------------------
# The static body -- unknown targets and undeclared actions are validation errors
# ---------------------------------------------------------------------------


async def test_an_unknown_resource_is_rejected_by_validation(client: AsyncClient):
    response = await client.post(
        "/batch-edit", json=[{"resource": "nope", "kind": "Delete", "id": 1}]
    )
    assert response.status_code == 422


async def test_an_unknown_kind_is_rejected_by_validation(client: AsyncClient):
    response = await client.post(
        "/batch-edit", json=[{"resource": "threads", "kind": "Upsert", "id": 1}]
    )
    assert response.status_code == 422


async def test_a_kind_the_resource_does_not_declare_is_rejected(client: AsyncClient):
    """A per-resource union means ``kind`` is checked against *its* resource."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    class ReadOnlyThread(SqlResource[Any, Any]):
        def get_supported_actions(self) -> frozenset[Action]:
            return frozenset({Action.READ, Action.SEARCH, Action.COUNT})

    read_only = ReadOnlyThread(Thread, session_factory=maker)
    writable = SqlResource(Tag, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[read_only, writable])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # ``threads`` declares no batch_edit, so it is not in the union at all.
            assert (
                await c.post(
                    "/batch-edit", json=[{"resource": "threads", "kind": "Delete", "id": 1}]
                )
            ).status_code == 422
            # ``tags`` does, so it is.
            assert (
                await c.post(
                    "/batch-edit",
                    json=[{"resource": "tags", "kind": "Create", "item": {"name": "x"}}],
                )
            ).status_code == 200
    await engine.dispose()


async def test_a_malformed_condition_is_a_validation_error_not_a_server_error(client: AsyncClient):
    response = await client.post(
        "/batch-edit",
        json=[{"resource": "threads", "kind": "Delete", "id": 1, "condition": "nope"}],
    )
    assert response.status_code == 422


async def test_a_hidden_resource_is_not_reachable():
    """A resource exposing nothing contributes no member, so it cannot be targeted."""

    class HiddenResource(SqlResource[Any, Any]):
        def get_exposed_resource(self) -> None:
            return None

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    hidden = HiddenResource(Tag, session_factory=maker)
    threads = SqlResource(Thread, session_factory=maker)

    manifest: Manifest = Manifest(resources=[hidden, threads])
    app = create_app(manifest)
    assert _resource_discriminators(app) == {"threads"}


# ---------------------------------------------------------------------------
# Mounting -- on by default, with an opt-out and a custom path
# ---------------------------------------------------------------------------


async def test_batch_edit_is_mounted_by_default(client: AsyncClient):
    assert "/batch-edit" in (await client.get("/openapi.json")).json()["paths"]


def test_batch_edit_can_be_disabled():
    threads = SqlResource(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    assert "/batch-edit" not in create_app(manifest, batch_edit=False).openapi()["paths"]


def test_batch_edit_can_be_mounted_on_a_custom_path():
    threads = SqlResource(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    paths = create_app(manifest, batch_edit_path="/ops/edits").openapi()["paths"]
    assert "/ops/edits" in paths
    assert "/batch-edit" not in paths


def test_add_to_app_mounts_it_on_a_user_owned_app():
    threads = SqlResource(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    app = FastAPI()
    add_to_app(manifest, app)
    assert "/batch-edit" in app.openapi()["paths"]


def test_a_manifest_with_no_writable_resource_mounts_nothing():
    class ReadOnlyThread(SqlResource[Any, Any]):
        def get_supported_actions(self) -> frozenset[Action]:
            return frozenset({Action.READ, Action.SEARCH, Action.COUNT})

    threads = ReadOnlyThread(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    assert "/batch-edit" not in create_app(manifest).openapi()["paths"]


def test_a_manifest_with_no_resources_mounts_nothing():
    manifest: Manifest = Manifest(resources=[])
    assert "/batch-edit" not in create_app(manifest).openapi()["paths"]


def test_a_developer_route_registered_first_is_not_clobbered():
    threads = SqlResource(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    app = FastAPI()

    @app.post("/batch-edit")
    async def mine() -> str:
        return "mine"

    add_to_app(manifest, app)
    routes = [r for r in app.routes if getattr(r, "path", None) == "/batch-edit"]
    assert len(routes) == 1
    assert routes[0].endpoint is mine


def test_register_batch_routes_is_usable_directly_on_a_router():
    threads = SqlResource(Thread, session_factory=_dummy_maker())
    manifest: Manifest = Manifest(resources=[threads])
    app = FastAPI()
    router = register_batch_routes(app, manifest, path="/custom-edits", prefix="/v1")
    assert router is not None
    assert "/v1/custom-edits" in app.openapi()["paths"]


async def test_create_app_threads_the_cap_through():
    """``batch_edit_max_edits`` on ``create_app`` actually reaches the handler."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, batch_edit_max_edits=1))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "a"}},
                    {"resource": "threads", "kind": "Create", "item": {"title": "b"}},
                ],
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_input"
            assert (await c.get("/threads/count")).json() == 0
    await engine.dispose()


# ---------------------------------------------------------------------------
# max_edits
# ---------------------------------------------------------------------------


async def test_a_batch_over_the_cap_is_rejected_before_any_write():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    app = create_app(manifest, batch_edit=False)
    register_batch_routes(app, manifest, max_edits=2)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "a"}},
                    {"resource": "threads", "kind": "Create", "item": {"title": "b"}},
                    {"resource": "threads", "kind": "Create", "item": {"title": "c"}},
                ],
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_input"
            # Rejected before the first write, so nothing was created.
            assert (await c.get("/threads/count")).json() == 0
    await engine.dispose()


async def test_the_cap_comes_from_config_when_not_passed(monkeypatch: pytest.MonkeyPatch):
    """``max_edits=None`` means "read the config", not "uncapped"."""
    monkeypatch.setenv("APP_MAX_BATCH_EDITS", "1")
    _reset_config_prefix()
    RequestLimitsConfig.clear_instance_cache()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "a"}},
                    {"resource": "threads", "kind": "Create", "item": {"title": "b"}},
                ],
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_input"
            assert (await c.get("/threads/count")).json() == 0
    await engine.dispose()

    RequestLimitsConfig.clear_instance_cache()
    _reset_config_prefix()


def test_a_non_positive_configured_cap_is_a_config_error(monkeypatch: pytest.MonkeyPatch):
    """``None`` is not an "unlimited" spelling, matching the other request caps."""
    monkeypatch.setenv("APP_MAX_BATCH_EDITS", "0")
    _reset_config_prefix()
    RequestLimitsConfig.clear_instance_cache()

    with pytest.raises(ResourceyConfigError):
        RequestLimitsConfig.get_instance()

    RequestLimitsConfig.clear_instance_cache()
    _reset_config_prefix()


# ---------------------------------------------------------------------------
# The builder seam -- every node goes through the builder
# ---------------------------------------------------------------------------


class _WrappingBuilder(DependencyBuilder):
    """Wraps every service it builds, recording the resources it was asked for."""

    _built: list[str] = PrivateAttr(default_factory=list)

    async def build_service(self, request: Request, resource: Resource[Any, Any]) -> Any:
        self._built.append(resource.get_resource_path())
        return await OpenDependencyBuilder().build_service(request, resource)

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Any:
        return OpenDependencyBuilder().get_service_dependency(resource)


async def test_the_builder_builds_each_targeted_resource(client: AsyncClient):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    builder = _WrappingBuilder()
    manifest: Manifest = Manifest(resources=[threads, messages])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "t"}},
                    {
                        "resource": "messages",
                        "kind": "Create",
                        "item": {"thread_id": 1, "body": "m"},
                    },
                ],
            )
    # One service per consecutive run, in the caller's order.
    assert builder._built == ["threads", "messages"]
    await engine.dispose()


async def test_a_custom_builder_wraps_the_service_the_route_uses(client: AsyncClient):
    """A builder that decorates the service is honoured, not bypassed.

    The route cannot use FastAPI's static dependency, so this is the property
    that keeps an authorization wrapper applied to a runtime-selected resource.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    seen: list[str] = []

    class RecordingService:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def __aenter__(self) -> Any:
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc: object) -> None:
            await self._inner.__aexit__(*exc)

        async def batch_edit(self, edits: Any) -> Any:
            seen.append("batch_edit")
            return await self._inner.batch_edit(edits)

    class Wrapping(DependencyBuilder):
        async def build_service(self, request: Request, resource: Resource[Any, Any]) -> Any:
            inner = await OpenDependencyBuilder().build_service(request, resource)
            return RecordingService(inner)

        def get_service_dependency(self, resource: Resource[Any, Any]) -> Any:
            return OpenDependencyBuilder().get_service_dependency(resource)

    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=Wrapping()))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[{"resource": "threads", "kind": "Create", "item": {"title": "t"}}],
            )
            assert response.status_code == 200
    assert seen == ["batch_edit"]
    await engine.dispose()


# ---------------------------------------------------------------------------
# Authorization -- the cross-resource route is not a bypass
# ---------------------------------------------------------------------------


class _RecordingTrigger(Trigger[Any, Any]):
    """Records each callback; module-level so the Trigger union can serialize it."""

    _fired: list[Any] = PrivateAttr(default_factory=list)

    async def callback(self, edits: Any, results: Any) -> None:
        self._fired.append(list(edits))


class _StubAuthenticator(Authenticator):
    """Authenticates every request as one fixed principal; never fails."""

    async def authenticate(self, request: Request) -> Any:
        from resourcey.auth.auth_principal import AuthResult, Principal

        return AuthResult.authenticated(Principal.service())


class _DenyAllResolver(PolicyResolver):
    """Grants no policies, so every action is refused."""

    async def resolve(self, resource: Resource[Any, Any], principal: Any) -> list[Any]:
        return []


async def test_an_authorized_app_still_authenticates_the_batch_route():
    """A missing credential is a ``401`` before any resource is touched."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    builder = AuthorizedDependencyBuilder()
    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[{"resource": "threads", "kind": "Create", "item": {"title": "t"}}],
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Not authenticated."
    await engine.dispose()


async def test_a_denying_policy_blocks_a_batch_edit():
    """A policy resolved per resource is enforced on the cross-resource route.

    This is the property that keeps the route from being an authorization
    bypass: the batch calls the builder, so the same wrapper that guards
    ``POST /threads`` guards ``POST /batch-edit``.

    A denied edit answers ``null`` rather than ``403``, matching the
    per-resource ``batch-edit`` contract (``AuthorizedService.batch_edit``): the
    position is a miss, and the row is never written.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    builder = AuthorizedDependencyBuilder(
        authenticator=_StubAuthenticator(),
        policy_resolver=_DenyAllResolver(),
        posture=Posture.OPTIONAL,
    )
    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[{"resource": "threads", "kind": "Create", "item": {"title": "t"}}],
            )
            assert response.status_code == 200
            assert response.json()[0]["result"] is None
            # Nothing was written: the policy refusal is real, not cosmetic.
            assert (await c.get("/threads/count")).json() == 0
    await engine.dispose()


class _ThreadsOnlyResolver(PolicyResolver):
    """Grants everything on ``threads`` and nothing on any other resource."""

    async def resolve(self, resource: Resource[Any, Any], principal: Any) -> list[Any]:
        if resource.get_resource_path() == "threads":
            return [AllowAll()]
        return []


async def test_policies_resolve_per_resource_within_one_batch():
    """One batch, two resources, two different verdicts.

    A single up-front check could not produce this: the resolver runs once per
    resource, so granting ``threads`` cannot leak a grant onto ``messages``.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    messages = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    builder = AuthorizedDependencyBuilder(
        authenticator=_StubAuthenticator(),
        policy_resolver=_ThreadsOnlyResolver(),
        posture=Posture.OPTIONAL,
    )
    manifest: Manifest = Manifest(resources=[threads, messages])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[
                    {"resource": "threads", "kind": "Create", "item": {"title": "t"}},
                    {
                        "resource": "messages",
                        "kind": "Create",
                        "item": {"thread_id": 1, "body": "b"},
                    },
                ],
            )
            assert response.status_code == 200
            assert response.json()[0]["result"]["title"] == "t"
            assert response.json()[1]["result"] is None
            assert (await c.get("/threads/count")).json() == 1
            assert (await c.get("/messages/count")).json() == 0
    await engine.dispose()


async def test_an_allowing_policy_permits_a_batch_edit():
    """The same wiring with a granting resolver lets the write through."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    builder = AuthorizedDependencyBuilder(
        authenticator=_StubAuthenticator(),
        posture=Posture.OPTIONAL,
    )
    manifest: Manifest = Manifest(resources=[threads])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[{"resource": "threads", "kind": "Create", "item": {"title": "t"}}],
            )
            assert response.status_code == 200
            assert (await c.get("/threads/count")).json() == 1
    await engine.dispose()


async def test_a_triggered_builder_fires_triggers_from_the_batch_route():
    """A ``TriggeredDependencyBuilder`` wraps the service the batch route builds."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    threads = SqlResource(Thread, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(BatchBase.metadata.create_all)

    recorder = _RecordingTrigger()
    builder = TriggeredDependencyBuilder(
        resource_triggers={"threads": [recorder]},
        background=False,
    )
    manifest: Manifest = Manifest(resources=[threads], managers=[builder])
    async with manifest:
        transport = ASGITransport(app=create_app(manifest, dependency_builder=builder))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post(
                "/batch-edit",
                json=[{"resource": "threads", "kind": "Create", "item": {"title": "t"}}],
            )
            assert response.status_code == 200
    assert len(recorder._fired) == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# A non-SQL backend -- rollback is not claimed where it does not exist
# ---------------------------------------------------------------------------


class Widget(BaseModel):
    id: int
    name: str


class _MutableListResource(ListResource[Any, Any]):
    """A list backend that admits writes, to prove the route is backend-agnostic."""

    def get_supported_actions(self) -> frozenset[Action]:
        return frozenset(
            {
                Action.CREATE,
                Action.READ,
                Action.SEARCH,
                Action.UPDATE,
                Action.DELETE,
                Action.BATCH_EDIT,
            }
        )


async def test_the_route_is_backend_agnostic():
    """A non-SQL backend is served by the same route with no special casing."""
    resource = _MutableListResource(models=[Widget(id=1, name="a")], model=Widget)
    manifest: Manifest = Manifest(resources=[resource])
    assert _resource_discriminators(create_app(manifest)) == {"widgets"}


def _resource_discriminators(app: FastAPI) -> set[str]:
    """The ``resource`` values the mounted batch body accepts.

    Read off the discriminator's ``mapping``: each member is a generated model
    carrying a ``resource`` literal, and the mapping is keyed by exactly those
    literals.
    """
    spec = app.openapi()
    schema = spec["paths"]["/batch-edit"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    return set(schema["items"]["discriminator"]["mapping"])


def _dummy_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_async_engine("sqlite+aiosqlite:///:memory:"))


@pytest.mark.parametrize("field", ["resource", "kind"])
async def test_a_node_missing_a_discriminator_is_rejected(client: AsyncClient, field: str):
    node = {"resource": "threads", "kind": "Delete", "id": 1}
    del node[field]
    response = await client.post("/batch-edit", json=[node])
    assert response.status_code == 422


async def test_a_batch_body_that_is_not_a_list_is_rejected(client: AsyncClient):
    response = await client.post("/batch-edit", json={"resource": "threads", "kind": "Delete"})
    assert response.status_code == 422
