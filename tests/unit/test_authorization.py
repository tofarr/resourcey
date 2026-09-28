"""Tests for the authorization system (issue #127).

These drive the real code paths — real ``Policy`` reductions, a real
``SqlResource`` over an in-memory SQLite table for the enforcement matrix, and
the real ``create_app`` transport with the real config-list API-key resource for
authentication. No mocks. Covered:

* the reduction of every built-in policy for every action, and the union
  vocabulary (``kind`` tagging / round-trip);
* the whole enforcement matrix (403 / 404 / empty / ``None`` positions) for
  ``AllowAll``, ``DenyAll`` and ``ReadOnly``;
* that ``AuthorizedService`` enters its inner, does not double-close an inner it
  was handed already entered, and delegates ``serialization_context``;
* ``AuthorizedDependencyBuilder`` composing authentication, its default
  ``AllowAll`` posture matching #118, and the auth check running before the
  target resource's storage is opened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME, ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import (
    config_api_key_resource,
    config_api_key_view,
)
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.auth.auth_authorized_service import AuthorizedService
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_policy import (
    AllowAll,
    DenyAll,
    Policy,
    PolicyResolver,
    ReadOnly,
)
from resourcey.core.manifest import Manifest
from resourcey.core.service import (
    Action,
    Create,
    Delete,
    ForbiddenError,
    NotFoundError,
    ServiceError,
    Update,
    normalize_action,
)
from resourcey.http.app import create_app
from resourcey.list.list_resource import ListResource
from resourcey.sql.sql_resource import SqlResource
from resourcey.util.search_filter import (
    AllFilter,
    EqFilter,
    NoMatchFilter,
    and_,
    attr,
)


class AuthzBase(DeclarativeBase):
    pass


class Item(AuthzBase):
    __tablename__ = "authz_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(100))


# ---------------------------------------------------------------------------
# Policy reduction
# ---------------------------------------------------------------------------


async def test_allow_all_reduces_to_all_for_every_action():
    for action in Action:
        assert isinstance(await AllowAll().to_search_filter(None, action), AllFilter)


async def test_deny_all_reduces_to_nomatch_for_every_action():
    for action in Action:
        assert isinstance(await DenyAll().to_search_filter(None, action), NoMatchFilter)


async def test_read_only_grants_read_like_and_denies_the_rest():
    policy = ReadOnly()
    for action in (Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ):
        assert isinstance(await policy.to_search_filter(None, action), AllFilter), action
    for action in (Action.CREATE, Action.UPDATE, Action.DELETE, Action.BATCH_EDIT):
        assert isinstance(await policy.to_search_filter(None, action), NoMatchFilter), action


def test_normalize_action_reduces_the_derived_members():
    assert normalize_action(Action.COUNT) is Action.SEARCH
    assert normalize_action(Action.BATCH_READ) is Action.READ
    assert normalize_action(Action.BATCH_EDIT) is Action.UPDATE
    for action in (Action.CREATE, Action.READ, Action.UPDATE, Action.DELETE, Action.SEARCH):
        assert normalize_action(action) is action


def test_policies_are_discriminated_and_round_trip():
    for policy in (AllowAll(), DenyAll(), ReadOnly()):
        dumped = policy.model_dump()
        assert dumped["kind"] == type(policy).__name__
        assert isinstance(Policy.model_validate(dumped), type(policy))


# ---------------------------------------------------------------------------
# AuthorizedService against a real SQL resource
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sql_resource() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], SqlResource[Any, Any]]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Item, session_factory=maker, path="items")
    async with engine.begin() as conn:
        await conn.run_sync(AuthzBase.metadata.create_all)
    yield maker, resource
    await engine.dispose()


def _dto(resource: SqlResource[Any, Any]) -> type[Any]:
    return resource.get_dto_type()


def _authorized(
    inner: Any, policy: Policy, resource: SqlResource[Any, Any]
) -> AuthorizedService[Any, Any]:
    return AuthorizedService(
        inner,
        policies=[policy],
        id_field=resource.get_id_field(),
        resource_name="items",
    )


async def _seed(resource: SqlResource[Any, Any], *labels: str) -> list[Any]:
    async with await resource.get_service({}) as service:
        made = [await service.create(_dto(resource)(label=label)) for label in labels]
        return made


async def test_allow_all_permits_the_full_action_surface(sql_resource):
    _maker, resource = sql_resource
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        created = await service.create(_dto(resource)(label="a"))
        assert created.id is not None
        assert (await service.read(created.id)).label == "a"
        updated = await service.update(_dto(resource)(id=created.id, label="b"))
        assert updated.label == "b"
        assert await service.count() == 1
        page = await service.search(limit=10)
        assert [item.label for item in page.items] == ["b"]
        batch = await service.batch_read([created.id, 999])
        assert batch[0].label == "b" and batch[1] is None
        edits = await service.batch_edit(
            [
                Create(item=_dto(resource)(label="c")),
                Update(item=_dto(resource)(id=created.id, label="d")),
                Delete(id=created.id),
            ]
        )
        assert edits[0].label == "c" and edits[1].label == "d" and edits[2] is None
        assert await service.count() == 1
        await service.delete(edits[0].id)
        assert await service.count() == 0


async def test_deny_all_enforces_the_matrix(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized(await resource.get_service({}), DenyAll(), resource) as service:
        with pytest.raises(ForbiddenError):
            await service.create(_dto(resource)(label="x"))
        # An existing row is out of scope -> 404 (not 403), so existence is hidden.
        with pytest.raises(NotFoundError):
            await service.read(seeded.id)
        with pytest.raises(NotFoundError):
            await service.update(_dto(resource)(id=seeded.id, label="y"))
        with pytest.raises(NotFoundError):
            await service.delete(seeded.id)
        # Collection endpoints do not error: an empty page / a count of 0.
        page = await service.search(limit=10)
        assert page.items == []
        assert await service.count() == 0
        # Batch positions are None (no read, no write).
        assert await service.batch_read([seeded.id]) == [None]
        assert await service.batch_edit([Update(item=_dto(resource)(id=seeded.id, label="z"))]) == [
            None
        ]


async def test_read_only_allows_reads_and_denies_writes(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized(await resource.get_service({}), ReadOnly(), resource) as service:
        assert (await service.read(seeded.id)).label == "a"
        assert await service.count() == 1
        assert [item.label for item in (await service.search(limit=10)).items] == ["a"]
        assert (await service.batch_read([seeded.id]))[0].label == "a"
        with pytest.raises(ForbiddenError):
            await service.create(_dto(resource)(label="x"))
        with pytest.raises(NotFoundError):
            await service.update(_dto(resource)(id=seeded.id, label="y"))
        with pytest.raises(NotFoundError):
            await service.delete(seeded.id)
        # A batch_edit update is denied (no write); the position is None.
        assert await service.batch_edit([Update(item=_dto(resource)(id=seeded.id, label="z"))]) == [
            None
        ]


async def test_search_pushes_the_permission_filter_into_the_request(sql_resource):
    _maker, resource = sql_resource
    await _seed(resource, "keep", "drop")
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        narrowed = await service.search(attr("label", EqFilter(value="keep")), limit=10)
        assert [item.label for item in narrowed.items] == ["keep"]
        assert await service.count(attr("label", EqFilter(value="keep"))) == 1


async def test_search_combines_request_and_denied_permission_as_nomatch(sql_resource):
    _maker, resource = sql_resource
    await _seed(resource, "a")
    async with _authorized(await resource.get_service({}), DenyAll(), resource) as service:
        # and_(NoMatch, request) is NoMatch regardless of the request filter.
        assert and_(NoMatchFilter(), attr("label", EqFilter(value="a"))) == NoMatchFilter()
        assert (await service.search(attr("label", EqFilter(value="a")), limit=10)).items == []


async def test_wrapper_enters_its_inner_and_closes_what_it_opened(sql_resource):
    _maker, resource = sql_resource
    inner = await resource.get_service({})
    service = _authorized(inner, AllowAll(), resource)
    async with service:
        assert inner.entered is True
    assert inner.entered is False


async def test_wrapper_leaves_an_already_entered_inner_open(sql_resource):
    _maker, resource = sql_resource
    inner = await resource.get_service({})
    async with inner:
        service = _authorized(inner, AllowAll(), resource)
        async with service:
            assert inner.entered is True
        # The dependency that opened the inner owns its close; the wrapper must not.
        assert inner.entered is True


async def test_wrapper_delegates_the_serialization_context(sql_resource):
    _maker, resource = sql_resource
    inner = await resource.get_service({})
    inner.set_serialization_context({"expose_secrets": True})
    service = _authorized(inner, AllowAll(), resource)
    assert service.serialization_context() == {"expose_secrets": True}


async def test_wrapper_requires_entry_before_use(sql_resource):
    _maker, resource = sql_resource
    service = _authorized(await resource.get_service({}), AllowAll(), resource)
    with pytest.raises(Exception, match="before entering"):
        await service.read(1)


async def test_update_without_an_identifier_reaches_the_inner(sql_resource):
    _maker, resource = sql_resource
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        # No id on the payload: the scope check steps aside and the inner service
        # raises its own error (not a 403).
        with pytest.raises(ServiceError, match="identifier"):
            await service.update(_dto(resource)(label="x"))


async def test_batch_edit_with_an_absent_or_idless_target_is_none(sql_resource):
    _maker, resource = sql_resource
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        edits = await service.batch_edit(
            [
                Update(item=_dto(resource)(id=999, label="x")),  # absent -> None
                Update(item=_dto(resource)(label="y")),  # no id -> None
            ]
        )
        assert edits == [None, None]


# ---------------------------------------------------------------------------
# AuthorizedDependencyBuilder over the real transport
# ---------------------------------------------------------------------------


class _RecordingList(ListResource[Any, int]):
    """A real list resource whose ``get_service`` records whether it was reached."""

    opened = False

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Any:
        type(self).opened = True
        raise AssertionError("target storage must not be opened before the auth check")


class Widget(BaseModel):
    """A plain Pydantic model for the recording list resource."""

    id: int
    label: str


class _FixedResolver(PolicyResolver):
    """A resolver returning one fixed policy — the pre-#131 single-policy behaviour."""

    policy: Policy

    async def resolve(self, resource: Any, principal: Any) -> list[Policy]:
        return [self.policy]


def _config_builder(policy: Policy) -> tuple[AuthorizedDependencyBuilder, Any]:
    cfg = ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", name="one", key=SecretStr("secret-one"))])
    inner = config_api_key_resource(cfg)
    view = config_api_key_view(inner)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=inner),
        policy_resolver=_FixedResolver(policy=policy),
    )
    return builder, view


@pytest_asyncio.fixture
async def authz_app() -> AsyncIterator[tuple[Any, Any, Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    items = SqlResource(Item, session_factory=maker, path="items")
    async with engine.begin() as conn:
        await conn.run_sync(AuthzBase.metadata.create_all)

    def make(policy: Policy) -> Any:
        builder, key_view = _config_builder(policy)
        manifest = Manifest(resources=[key_view, items])
        return create_app(manifest, dependency_builder=builder), manifest

    yield make
    await engine.dispose()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_default_builder_posture_matches_api_key_full_access(authz_app):
    app, manifest = authz_app(AllowAll())
    async with manifest, await _client(app) as client:
        headers = {API_KEY_HEADER_NAME: "secret-one"}
        created = await client.post("/items", json={"label": "a"}, headers=headers)
        assert created.status_code == 201
        item_id = created.json()["id"]
        assert (await client.get(f"/items/{item_id}", headers=headers)).status_code == 200
        assert (await client.get("/items", headers=headers)).status_code == 200


async def test_auth_check_runs_before_target_storage_with_an_invalid_key():
    _RecordingList.opened = False
    target = _RecordingList([Widget(id=1, label="a")], path="items")
    builder, key_view = _config_builder(AllowAll())
    manifest = Manifest(resources=[key_view, target])
    app = create_app(manifest, dependency_builder=builder)
    async with manifest, await _client(app) as client:
        response = await client.get("/items")
    assert response.status_code == 401
    assert _RecordingList.opened is False


async def test_deny_all_over_http_rejects_writes_and_hides_rows(authz_app):
    app, manifest = authz_app(DenyAll())
    async with manifest, await _client(app) as client:
        headers = {API_KEY_HEADER_NAME: "secret-one"}
        # A create is a 403.
        forbidden = await client.post("/items", json={"label": "a"}, headers=headers)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "forbidden"
        # A collection read is an empty page, not an error.
        empty = await client.get("/items", headers=headers)
        assert empty.status_code == 200
        assert empty.json()["items"] == []
        # A by-id read of an absent row is a 404.
        missing = await client.get("/items/1", headers=headers)
        assert missing.status_code == 404


async def test_read_only_over_http_allows_reads_denies_writes(authz_app):
    app, manifest = authz_app(ReadOnly())
    async with manifest, await _client(app) as client:
        headers = {API_KEY_HEADER_NAME: "secret-one"}
        forbidden = await client.post("/items", json={"label": "a"}, headers=headers)
        assert forbidden.status_code == 403
        assert (await client.get("/items", headers=headers)).status_code == 200


async def test_absent_key_is_unauthorized_under_any_policy(authz_app):
    app, manifest = authz_app(AllowAll())
    async with manifest, await _client(app) as client:
        assert (await client.get("/items")).status_code == 401
