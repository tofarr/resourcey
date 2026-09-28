"""Tests for simple, per-app roles and the centralized role -> policy translation.

These drive the real code paths — a real ``EncryptionService`` over a JWE, real
``SqlResource``s over an in-memory SQLite table, the real config-list / DB-backed
API-key resources, and the real ``create_app`` transport. No mocks. Covered:

* the ``AppRole`` vocabulary and the string conversion seam;
* role carriage for **both** credential types (API key and cookie JWE claims);
* the ``Owner`` (own-rows-only) policy reduction;
* ``RolePolicyResolver`` — global + per-resource mapping, union of several roles,
  and the fail-closed default;
* the builder wiring a role-based resolver onto the transport, including
  "read all of X but only own rows of Y".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import String, Uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME, ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import (
    ApiKey,
    config_api_key_resource,
    config_api_key_view,
    stored_api_key_resource,
)
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.auth.auth_authorized_service import AuthorizedService
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_cookie import CookieAuthenticator
from resourcey.auth.auth_policy import AllowAll, Owner, Policy, ReadOnly
from resourcey.auth.auth_principal import PRINCIPAL_CTX_KEY, Principal, PrincipalKind
from resourcey.auth.auth_role import (
    AppRole,
    RolePolicyResolver,
    role_key,
    role_keys,
    roles_from_credential,
)
from resourcey.core.manifest import Manifest
from resourcey.core.service import Action, NotFoundError
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService
from resourcey.http.app import create_app
from resourcey.list.list_resource import ListResource
from resourcey.sql.sql_resource import SqlResource
from resourcey.sql.sql_service import SqlService
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import AllFilter, AttrFilter, EqFilter, NoMatchFilter

ALICE = uuid4()
BOB = uuid4()


class Role(AppRole):
    """An app's role vocabulary (per-app, not global)."""

    ADMIN = "ADMIN"
    USER = "USER"


class Dummy(BaseModel):
    """A trivial Pydantic model for path-only list resources."""

    id: int
    name: str = ""


def _list(path: str) -> ListResource[Any, Any]:
    """A real list resource serving no items, used only for its path."""
    return ListResource([], model=Dummy, path=path)


# ---------------------------------------------------------------------------
# Role vocabulary
# ---------------------------------------------------------------------------


def test_app_role_is_a_plain_app_scoped_string():
    assert Role.ADMIN == "ADMIN"
    assert role_key(Role.ADMIN) == "ADMIN"
    assert role_key("ADMIN") == "ADMIN"
    assert role_keys({Role.ADMIN, Role.USER}) == frozenset({"ADMIN", "USER"})


def test_roles_from_credential_normalises_the_transported_forms():
    assert roles_from_credential(None) == frozenset()
    assert roles_from_credential("") == frozenset()
    assert roles_from_credential("ADMIN") == frozenset({"ADMIN"})
    assert roles_from_credential("ADMIN, USER") == frozenset({"ADMIN", "USER"})
    assert roles_from_credential(["ADMIN", "USER"]) == frozenset({"ADMIN", "USER"})


# ---------------------------------------------------------------------------
# Owner policy
# ---------------------------------------------------------------------------


async def test_owner_scopes_reads_and_writes_to_the_caller():
    owner = Owner(owner_field="owner_id")
    for action in (Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ):
        assert isinstance(await owner.to_search_filter(ALICE, action), AttrFilter), action
    for action in (Action.UPDATE, Action.DELETE, Action.BATCH_EDIT):
        assert isinstance(await owner.to_search_filter(ALICE, action), AttrFilter), action
    # Create is unscoped (the row has no owner yet).
    assert isinstance(await owner.to_search_filter(ALICE, Action.CREATE), AllFilter)


async def test_owner_denies_an_anonymous_caller():
    owner = Owner()
    for action in Action:
        assert isinstance(await owner.to_search_filter(None, action), NoMatchFilter), action


async def test_owner_reduces_to_the_owner_equality_filter():
    filt = await Owner(owner_field="owner_id").to_search_filter(ALICE, Action.READ)
    assert filt == AttrFilter(attribute="owner_id", filter=EqFilter(value=ALICE))


def test_owner_is_discriminated_and_round_trips_with_its_field():
    dumped = Owner(owner_field="owner_id").model_dump()
    assert dumped["kind"] == "Owner"
    restored = Policy.model_validate(dumped)
    assert isinstance(restored, Owner)
    assert restored.owner_field == "owner_id"


# ---------------------------------------------------------------------------
# RolePolicyResolver
# ---------------------------------------------------------------------------


def _resolver() -> RolePolicyResolver:
    return RolePolicyResolver(
        role_policies={"ADMIN": [AllowAll()], "READER": [ReadOnly()]},
        resource_role_policies={
            "threads": {"USER": [ReadOnly()]},
            "messages": {"USER": [Owner(owner_field="owner_id")]},
        },
        default=[],
    )


async def test_resolver_maps_roles_globally_and_per_resource():
    resolver = _resolver()
    admin = Principal.user(ALICE, roles=frozenset({"ADMIN"}))
    user = Principal.user(ALICE, roles=frozenset({"USER"}))
    assert [type(p).__name__ for p in await resolver.resolve(_list("threads"), admin)] == [
        "AllowAll"
    ]
    assert [type(p).__name__ for p in await resolver.resolve(_list("threads"), user)] == [
        "ReadOnly"
    ]
    messages = await resolver.resolve(_list("messages"), user)
    assert [type(p).__name__ for p in messages] == ["Owner"]
    assert messages[0].owner_field == "owner_id"  # type: ignore[attr-defined]


async def test_resolver_unions_multiple_roles():
    resolver = _resolver()
    both = Principal.user(ALICE, roles=frozenset({"READER", "USER"}))
    policies = await resolver.resolve(_list("messages"), both)
    assert {type(p).__name__ for p in policies} == {"ReadOnly", "Owner"}


async def test_resolver_is_fail_closed_for_an_unroled_or_unknown_caller():
    resolver = _resolver()
    assert await resolver.resolve(_list("threads"), Principal.user(ALICE)) == []
    assert await resolver.resolve(_list("threads"), Principal.anonymous()) == []
    unknown = Principal.user(ALICE, roles=frozenset({"GHOST"}))
    assert await resolver.resolve(_list("threads"), unknown) == []


async def test_resolver_resource_default_overrides_the_global_default():
    resolver = RolePolicyResolver(
        resource_defaults={"threads": [ReadOnly()]},
        default=[],
    )
    assert [type(p).__name__ for p in await resolver.resolve(_list("threads"), None)] == [
        "ReadOnly"
    ]
    assert await resolver.resolve(_list("messages"), None) == []


def test_resolver_round_trips_through_the_union_machinery():
    dumped = _resolver().model_dump()
    assert dumped["kind"] == "RolePolicyResolver"
    restored = RolePolicyResolver.model_validate(dumped)
    messages = restored.resource_role_policies["messages"]["USER"]
    assert isinstance(messages[0], Owner)


# ---------------------------------------------------------------------------
# Credential-carried roles
# ---------------------------------------------------------------------------


def _keys(entries: list[ApiKeyConfig]) -> ApiKeysConfig:
    return ApiKeysConfig(api_keys=entries)


async def _authenticate_via_http(authenticator: Any, key: str) -> Principal:
    """Run the authenticator over a real FastAPI app and return its principal."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        result = await authenticator.authenticate(request)
        assert result.principal is not None
        p = result.principal
        return {"id": str(p.id), "kind": p.kind.value, "roles": sorted(p.roles)}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/whoami", headers={API_KEY_HEADER_NAME: key})
    body = response.json()
    return Principal(
        id=None if body["id"] == "None" else UUID(body["id"]),
        kind=PrincipalKind(body["kind"]),
        roles=frozenset(body["roles"]),
    )


async def test_config_key_carries_its_roles_without_a_store_lookup():
    cfg = _keys(
        [
            ApiKeyConfig(
                id="k1",
                key=SecretStr("secret-one"),
                principal_id=str(ALICE),
                roles=["USER", "READER"],
            )
        ]
    )
    authenticator = ApiKeyAuthenticator(key_resource=config_api_key_resource(cfg))
    principal = await _authenticate_via_http(authenticator, "secret-one")
    assert principal.roles == frozenset({"USER", "READER"})
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.id == ALICE


@pytest_asyncio.fixture
async def key_session() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(ApiKey.__table__.create)
    yield maker
    await engine.dispose()


async def test_stored_key_carries_its_roles(key_session):
    inner = stored_api_key_resource(session_factory=key_session)
    async with await inner.get_service({}) as service:
        created = await service.create(inner.get_dto_type()(name="svc", roles=["ADMIN"]))
        raw = created.key.get_secret_value()
    authenticator = ApiKeyAuthenticator(key_resource=inner)
    principal = await _authenticate_via_http(authenticator, raw)
    assert principal.roles == frozenset({"ADMIN"})


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value=SecretStr("test-secret"))
        )
    )


async def test_cookie_carries_its_roles_in_the_jwe_claims():
    service = _encryption()
    token = service.create_jwe_token(
        {
            "sub": str(ALICE),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "roles": ["ADMIN"],
        }
    )
    authenticator = CookieAuthenticator(cookie_name="sid", encryption_service=service)
    app = FastAPI()

    @app.get("/whoami")
    async def whoami() -> dict[str, Any]:
        # The dependency's Request carries the cookie from the client.
        raise NotImplementedError

    # Drive the authenticator directly with a minimal request-like object.
    class _Req:
        def __init__(self) -> None:
            self.cookies = {"sid": token}

    result = await authenticator.authenticate(_Req())  # type: ignore[arg-type]
    assert result.principal is not None
    assert result.principal.roles == frozenset({"ADMIN"})


# ---------------------------------------------------------------------------
# The builder wires the role resolver onto the transport
# ---------------------------------------------------------------------------


class OwnerBase(DeclarativeBase):
    pass


class Note(OwnerBase):
    """A row owned by a principal — the "own rows" resource."""

    __tablename__ = "role_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    text: Mapped[str] = mapped_column(String(100))


async def test_owner_enforces_own_rows_against_real_sql():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Note, session_factory=maker, path="notes")
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        async with await resource.get_service({}) as service:
            alices = await service.create(resource.get_dto_type()(owner_id=ALICE, text="alice"))
            bobs = await service.create(resource.get_dto_type()(owner_id=BOB, text="bob"))

        inner = await resource.get_service({})
        async with AuthorizedService(
            inner,
            policies=[Owner(owner_field="owner_id")],
            id_field=resource.get_id_field(),
            resource_name="notes",
            user_id=ALICE,
        ) as service:
            assert (await service.read(alices.id)).text == "alice"
            with pytest.raises(NotFoundError):
                await service.read(bobs.id)
            page = await service.search(limit=10)
            assert [n.text for n in page.items] == ["alice"]
            with pytest.raises(NotFoundError):
                await service.delete(bobs.id)
            # Create is unscoped: an authenticated caller may create.
            created = await service.create(resource.get_dto_type()(owner_id=None, text="new"))
            assert created.text == "new"
    finally:
        await engine.dispose()


async def test_role_resolver_end_to_end_reads_all_of_x_but_own_rows_of_y():
    """A USER role reads every ``broad`` row but only its own ``narrow`` rows."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    broad = SqlResource(Note, session_factory=maker, path="broad")
    narrow = SqlResource(Note, session_factory=maker, path="narrow")
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        async with await broad.get_service({}) as service:
            await service.create(broad.get_dto_type()(owner_id=ALICE, text="a"))
            await service.create(broad.get_dto_type()(owner_id=BOB, text="b"))

        resolver = RolePolicyResolver(
            resource_role_policies={
                "broad": {"USER": [ReadOnly()]},
                "narrow": {"USER": [Owner(owner_field="owner_id")]},
            }
        )
        cfg = _keys(
            [
                ApiKeyConfig(
                    id="k1",
                    key=SecretStr("user-key"),
                    principal_id=str(ALICE),
                    roles=["USER"],
                )
            ]
        )
        inner = config_api_key_resource(cfg)
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(key_resource=inner),
            policy_resolver=resolver,
        )
        manifest = Manifest(resources=[config_api_key_view(inner), broad, narrow])
        app = create_app(manifest, dependency_builder=builder)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {API_KEY_HEADER_NAME: "user-key"}
                # X: reads every row.
                broad_page = (await client.get("/broad", headers=headers)).json()
                assert {n["text"] for n in broad_page["items"]} == {"a", "b"}
                # Y: reads only its own rows.
                narrow_page = (await client.get("/narrow", headers=headers)).json()
                assert [n["text"] for n in narrow_page["items"]] == ["a"]
    finally:
        await engine.dispose()


async def test_owner_policy_and_owner_stamping_end_to_end():
    """The builder publishes the principal so an Owner-scoped resource stamps rows.

    The ``Owner`` policy leaves *create* unscoped (a new row has no owner yet),
    so the resource service reads the principal the builder stored on the
    call-scoped ``ctx`` and writes it as the row's owner. A subsequent read by
    the same caller then matches its own row.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    class StampingResource(SqlResource[Any, Any]):
        def make_service(self, ctx: Any, session_factory: Any) -> Any:
            return _StampingService(self, ctx, session_factory)

    resource = StampingResource(Note, session_factory=maker, path="stamped")
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        cfg = _keys(
            [
                ApiKeyConfig(
                    id="k1", key=SecretStr("user-key"), principal_id=str(ALICE), roles=["USER"]
                )
            ]
        )
        inner = config_api_key_resource(cfg)
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(key_resource=inner),
            policy_resolver=RolePolicyResolver(
                resource_role_policies={"stamped": {"USER": [Owner(owner_field="owner_id")]}}
            ),
        )
        manifest = Manifest(resources=[config_api_key_view(inner), resource])
        app = create_app(manifest, dependency_builder=builder)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {API_KEY_HEADER_NAME: "user-key"}
                # Created without an owner; the service stamps the principal id.
                created = await client.post("/stamped", json={"text": "mine"}, headers=headers)
                assert created.status_code == 201
                assert created.json()["owner_id"] == str(ALICE)
                # ...and is then readable by that same caller.
                listed = (await client.get("/stamped", headers=headers)).json()
                assert [n["text"] for n in listed["items"]] == ["mine"]
    finally:
        await engine.dispose()


class _StampingService(SqlService[Any, Any]):
    """A service that stamps ``owner_id`` from the principal published on the ctx."""

    async def create(self, payload: Any) -> Any:
        principal = self._ctx.get(PRINCIPAL_CTX_KEY)
        if principal is not None and getattr(payload, "owner_id", MISSING) in (MISSING, None):
            payload = payload.model_copy(update={"owner_id": principal.id})
        return await super().create(payload)


async def test_admin_role_grants_full_access_end_to_end():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Note, session_factory=maker, path="notes")
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        cfg = _keys([ApiKeyConfig(id="k1", key=SecretStr("admin-key"), roles=["ADMIN"])])
        inner = config_api_key_resource(cfg)
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(key_resource=inner),
            policy_resolver=RolePolicyResolver(role_policies={"ADMIN": [AllowAll()]}),
        )
        manifest = Manifest(resources=[config_api_key_view(inner), resource])
        app = create_app(manifest, dependency_builder=builder)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {API_KEY_HEADER_NAME: "admin-key"}
                created = await client.post(
                    "/notes", json={"owner_id": str(BOB), "text": "admin made"}, headers=headers
                )
                assert created.status_code == 201
                listed = (await client.get("/notes", headers=headers)).json()
                assert [n["text"] for n in listed["items"]] == ["admin made"]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Caller-scoped responses are private (a shared cache must not replay them)
# ---------------------------------------------------------------------------


def _owner_scoped_app(maker: Any) -> Any:
    """An ``Owner``-scoped ``/notes`` app plus its two principals' contexts."""
    resource = SqlResource(Note, session_factory=maker, path="notes")
    cfg = _keys(
        [
            ApiKeyConfig(
                id="alice", key=SecretStr("alice-key"), principal_id=str(ALICE), roles=["USER"]
            ),
            ApiKeyConfig(id="bob", key=SecretStr("bob-key"), principal_id=str(BOB), roles=["USER"]),
        ]
    )
    inner = config_api_key_resource(cfg)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=inner),
        policy_resolver=RolePolicyResolver(
            resource_role_policies={"notes": {"USER": [Owner(owner_field="owner_id")]}}
        ),
    )
    manifest = Manifest(resources=[config_api_key_view(inner), resource])
    return create_app(manifest, dependency_builder=builder), manifest, resource


def test_policy_privacy_flags_are_class_level():
    # The principal-independent built-ins opt out of caller-scoping so their
    # shared-cache optimisations survive; an unclassified policy is caller-scoped.
    assert AllowAll().scopes_to_caller is False
    assert ReadOnly().scopes_to_caller is False
    assert Owner(owner_field="owner_id").scopes_to_caller is True


async def test_owner_scoped_response_is_caller_private():
    """An ``Owner``-scoped search must not be stored/shared by a shared cache.

    The ETag alone cannot stop a shared cache revalidating Bob's cached body with
    Alice's client presenting Bob's validator, so the response must carry
    ``Cache-Control: private``. Regression test for the cross-principal 304 leak.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app, manifest, resource = _owner_scoped_app(maker)
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        async with await resource.get_service({}) as service:
            await service.create(resource.get_dto_type()(owner_id=ALICE, text="alice"))
            await service.create(resource.get_dto_type()(owner_id=BOB, text="bob"))
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                alice = await client.get("/notes", headers={API_KEY_HEADER_NAME: "alice-key"})
                bob = await client.get("/notes", headers={API_KEY_HEADER_NAME: "bob-key"})
                assert [n["text"] for n in alice.json()["items"]] == ["alice"]
                assert [n["text"] for n in bob.json()["items"]] == ["bob"]
                assert "private" in alice.headers["cache-control"]
                assert "private" in bob.headers["cache-control"]
    finally:
        await engine.dispose()


async def test_principal_independent_role_response_keeps_shared_caching():
    """A ``ReadOnly`` (principal-independent) role must not be forced private.

    Guards against over-marking: the privacy flag follows the *policies*, so a
    role that grants the same rows to everyone keeps its shared-cache validator.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Note, session_factory=maker, path="notes")
    async with engine.begin() as conn:
        await conn.run_sync(OwnerBase.metadata.create_all)
    try:
        cfg = _keys(
            [
                ApiKeyConfig(
                    id="r", key=SecretStr("reader-key"), principal_id=str(ALICE), roles=["READER"]
                )
            ]
        )
        inner = config_api_key_resource(cfg)
        builder = AuthorizedDependencyBuilder(
            authenticator=ApiKeyAuthenticator(key_resource=inner),
            policy_resolver=RolePolicyResolver(
                resource_role_policies={"notes": {"READER": [ReadOnly()]}}
            ),
        )
        manifest = Manifest(resources=[config_api_key_view(inner), resource])
        app = create_app(manifest, dependency_builder=builder)
        async with manifest:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/notes", headers={API_KEY_HEADER_NAME: "reader-key"})
                assert "private" not in response.headers["cache-control"]
    finally:
        await engine.dispose()
