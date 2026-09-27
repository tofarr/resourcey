"""Tests for the store-backed RBAC rung (issue #133, Part 3 of the auth roadmap).

These drive the real code paths — real ``SqlResource``s over in-memory SQLite
tables, the real RBAC tables, the real ``SqlRbacStore`` / ``RbacPolicyResolver``,
the real ``AuthorizedService`` and the real ``create_app`` transport. No mocks.

Covered:

* the RBAC resource set and its per-request resolution (groups to roles to
  permissions), scoped to the target resource;
* multiple roles OR-combined (the union model — a ``DenyAll`` never overrides a
  grant);
* ownership scoping (``Owner`` / ``Acl``) including the 404-vs-403 distinction;
* group-membership scoping (``GroupMember`` with pre-bound membership);
* fail-closed on no permissions / anonymous principals;
* the credential-threshold freshness bound (``cache_ttl`` + ``invalidate``);
* the materialized-ACL escape hatch and its enumeration cap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import String, Uuid, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.auth.auth_api_key import API_KEY_HEADER_NAME, ApiKeyAuthenticator
from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.auth.auth_policy import (
    Acl,
    AllowAll,
    DenyAll,
    GroupMember,
    Owner,
    ReadOnly,
)
from resourcey.v2.auth.auth_principal import Principal
from resourcey.v2.auth.auth_rbac import (
    Group,
    GroupRole,
    GroupUser,
    RbacBase,
    ResourceAcl,
    Role,
    RolePermission,
    User,
    policy_to_json,
)
from resourcey.v2.auth.auth_rbac_resolver import RbacPolicyResolver
from resourcey.v2.auth.auth_rbac_store import ACL_MAX_IDS, SqlRbacStore
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Action
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.util.search_filter import AllFilter, AttrFilter, InFilter, NoMatchFilter

ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ADMINS = UUID("11111111-1111-1111-1111-111111111111")
READERS = UUID("22222222-2222-2222-2222-222222222222")


class NoteBase(DeclarativeBase):
    """The owner-scoped table the RBAC tests protect."""


class Note(NoteBase):
    """A note owned by ``owner_id`` (a real UUID principal)."""

    __tablename__ = "rbac_notes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    text: Mapped[str] = mapped_column(String, nullable=False)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An in-memory SQLite session maker with both metadata sets created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(NoteBase.metadata.create_all)
        await conn.run_sync(RbacBase.metadata.create_all)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _grant(
    maker: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    group_id: UUID,
    role_name: str,
    resource: str,
    permission: Any,
) -> None:
    """Seed one grant: user joins a group, the group holds a role, the role a policy."""
    role_id = uuid4()
    async with maker() as session:
        if await _group_exists(session, group_id) is None:
            session.add(Group(id=group_id, name=f"g-{group_id}"))
        session.add(GroupUser(id=uuid4(), group_id=group_id, user_id=user_id))
        session.add(Role(id=role_id, name=role_name))
        session.add(GroupRole(id=uuid4(), group_id=group_id, role_id=role_id))
        session.add(
            RolePermission(
                id=uuid4(),
                role_id=role_id,
                resource=resource,
                permission=policy_to_json(permission),
            )
        )
        await session.commit()


async def _group_exists(session: AsyncSession, group_id: UUID) -> Any:
    return (await session.execute(select(Group).where(Group.id == group_id))).scalars().first()


async def _add_user(
    maker: async_sessionmaker[AsyncSession], user_id: UUID, group_id: UUID | None = None
) -> None:
    """Seed a user and (optionally) a group they belong to."""
    async with maker() as session:
        session.add(User(id=user_id, email=f"{user_id}@x", username=str(user_id)))
        if group_id is not None:
            session.add(Group(id=group_id, name=f"g-{group_id}"))
            session.add(GroupUser(id=uuid4(), group_id=group_id, user_id=user_id))
        await session.commit()


def _make_resolver(maker: async_sessionmaker[AsyncSession], **kwargs: Any) -> RbacPolicyResolver:
    return RbacPolicyResolver(store=SqlRbacStore(maker), **kwargs)


def _app(
    resource: Resource[Any, Any], resolver: Any, key: str, principal_id: UUID
) -> tuple[Manifest, Any]:
    """An app over ``resource``, authenticated by a config key as ``principal_id``."""
    keys = ApiKeysConfig(
        api_keys=[ApiKeyConfig(id="k1", key=SecretStr(key), principal_id=str(principal_id))]
    )
    inner = config_api_key_resource(keys)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=inner),
        policy_resolver=resolver,
    )
    manifest = Manifest(resources=[config_api_key_view(inner), resource])
    return manifest, create_app(manifest, dependency_builder=builder)


# ---------------------------------------------------------------------------
# The store: resolution through real tables
# ---------------------------------------------------------------------------


async def test_store_resolves_policies_through_real_tables(maker: Any) -> None:
    """user -> group -> role -> permission resolves the stored policy for the resource."""
    await _add_user(maker, ALICE, READERS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=READERS,
        role_name="reader",
        resource="rbac-notes",
        permission=Owner(owner_field="owner_id"),
    )
    store = SqlRbacStore(maker)
    policies = await store.policies_for(ALICE, "rbac-notes")
    assert len(policies) == 1
    assert isinstance(policies[0], Owner)
    assert policies[0].owner_field == "owner_id"
    # Scoped to the target resource: another resource yields nothing.
    assert await store.policies_for(ALICE, "other") == []
    # And another principal yields nothing.
    assert await store.policies_for(BOB, "rbac-notes") == []


async def test_store_distinct_collapses_duplicate_policies(maker: Any) -> None:
    """Two roles granting the *same* policy collapse to one row (SELECT DISTINCT)."""
    await _add_user(maker, ALICE, ADMINS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="admin",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="superuser",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    policies = await SqlRbacStore(maker).policies_for(ALICE, "rbac-notes")
    assert len(policies) == 1


async def test_store_groups_for(maker: Any) -> None:
    await _add_user(maker, ALICE, READERS)
    assert await SqlRbacStore(maker).groups_for(ALICE) == frozenset({READERS})
    assert await SqlRbacStore(maker).groups_for(BOB) == frozenset()


# ---------------------------------------------------------------------------
# Resolver: per-request resolution + fail-closed
# ---------------------------------------------------------------------------


async def test_resolver_anonymous_is_fail_closed(maker: Any) -> None:
    resolver = _make_resolver(maker)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    assert await resolver.resolve(resource, None) == []
    assert await resolver.resolve(resource, Principal(id=None, kind="anonymous")) == []


async def test_resolver_binds_group_membership(maker: Any) -> None:
    """A ``GroupMember`` gets the principal's groups bound so its reduction can branch."""
    await _add_user(maker, ALICE, READERS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=READERS,
        role_name="reader",
        resource="rbac-notes",
        permission=GroupMember(group_ids=[READERS], on_match=AllowAll()),
    )
    resolver = _make_resolver(maker)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    policies = await resolver.resolve(resource, Principal(id=ALICE, kind="user"))
    assert len(policies) == 1
    reduced = await policies[0].to_search_filter(ALICE, Action.READ)
    assert isinstance(reduced, AllFilter)


# ---------------------------------------------------------------------------
# End-to-end: ownership scoping + 404-vs-403
# ---------------------------------------------------------------------------


async def test_owner_scoping_end_to_end(maker: Any) -> None:
    """A role's ``Owner`` policy scopes reads/updates to own rows; others are 404."""
    await _add_user(maker, ALICE, READERS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=READERS,
        role_name="reader",
        resource="rbac-notes",
        permission=Owner(owner_field="owner_id"),
    )
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    manifest, app = _app(resource, resolver, "alice-key", ALICE)
    async with await resource.get_service({}) as service:
        alices = await service.create(resource.get_dto_type()(owner_id=ALICE, text="alice"))
        bobs = await service.create(resource.get_dto_type()(owner_id=BOB, text="bob"))
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            page = (await client.get("/rbac-notes", headers=h)).json()
            assert [n["text"] for n in page["items"]] == ["alice"]
            assert (await client.get(f"/rbac-notes/{bobs.id}", headers=h)).status_code == 404
            assert (await client.get(f"/rbac-notes/{alices.id}", headers=h)).status_code == 200
            # A denied create (Owner grants create unscoped) is allowed here...
            created = await client.post("/rbac-notes", json={"text": "mine"}, headers=h)
            assert created.status_code == 201


async def test_denied_create_is_403(maker: Any) -> None:
    """A role whose only policy denies create gets a 403 (not 404)."""
    await _add_user(maker, ALICE, READERS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=READERS,
        role_name="reader",
        resource="rbac-notes",
        permission=ReadOnly(),
    )
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    manifest, app = _app(resource, resolver, "alice-key", ALICE)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            assert (
                await client.post("/rbac-notes", json={"text": "x"}, headers=h)
            ).status_code == 403
            # A collection read is emptied, never a 403.
            assert (await client.get("/rbac-notes", headers=h)).json()["items"] == []


async def test_unroled_principal_is_fail_closed_end_to_end(maker: Any) -> None:
    """A user with no grants resolves to no policies, so every action is denied."""
    await _add_user(maker, ALICE)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    manifest, app = _app(resource, resolver, "alice-key", ALICE)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            assert (await client.get("/rbac-notes", headers=h)).json()["items"] == []
            assert (
                await client.post("/rbac-notes", json={"text": "x"}, headers=h)
            ).status_code == 403


async def test_multiple_roles_or_combine_no_deny_override(maker: Any) -> None:
    """Two roles: one read-only, one ``DenyAll`` — the grant survives (union model)."""
    await _add_user(maker, ALICE)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=READERS,
        role_name="reader",
        resource="rbac-notes",
        permission=ReadOnly(),
    )
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="denier",
        resource="rbac-notes",
        permission=DenyAll(),
    )
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    manifest, app = _app(resource, resolver, "alice-key", ALICE)
    async with await resource.get_service({}) as service:
        await service.create(resource.get_dto_type()(owner_id=ALICE, text="visible"))
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            page = (await client.get("/rbac-notes", headers=h)).json()
            assert [n["text"] for n in page["items"]] == ["visible"]


async def test_admin_role_has_full_access(maker: Any) -> None:
    await _add_user(maker, ALICE)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="admin",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    manifest, app = _app(resource, resolver, "alice-key", ALICE)
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            resp = await client.post("/rbac-notes", json={"text": "admin"}, headers=h)
            assert resp.status_code == 201
            listed = (await client.get("/rbac-notes", headers=h)).json()
            assert [n["text"] for n in listed["items"]] == ["admin"]


# ---------------------------------------------------------------------------
# Group-membership policy end-to-end
# ---------------------------------------------------------------------------


async def test_group_member_policy_end_to_end(maker: Any) -> None:
    """A ``GroupMember`` grant applies to members and not to non-members."""
    await _add_user(maker, ALICE, ADMINS)
    await _add_user(maker, BOB, READERS)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="moderator",
        resource="rbac-notes",
        # The permission targets ADMINS; the resolver binds membership.
        permission=GroupMember(group_ids=[ADMINS], on_match=AllowAll(), on_create=AllowAll()),
    )
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = _make_resolver(maker)
    # Alice (a member) may write; Bob (not a member) resolves to no policy.
    alice_manifest, alice_app = _app(resource, resolver, "alice-key", ALICE)
    async with alice_manifest:
        transport = ASGITransport(app=alice_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "alice-key"}
            assert (
                await client.post("/rbac-notes", json={"text": "m"}, headers=h)
            ).status_code == 201

    bob_manifest, bob_app = _app(resource, resolver, "bob-key", BOB)
    async with bob_manifest:
        transport = ASGITransport(app=bob_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = {API_KEY_HEADER_NAME: "bob-key"}
            # Bob holds no grant at all (his group has no role), so he is denied.
            assert (
                await client.post("/rbac-notes", json={"text": "n"}, headers=h)
            ).status_code == 403


# ---------------------------------------------------------------------------
# Materialized ACL escape hatch
# ---------------------------------------------------------------------------


async def test_acl_policy_reduces_to_in_filter(maker: Any) -> None:
    ids = (uuid4(), uuid4())
    policy = Acl(ids=frozenset(ids))
    reduced = await policy.to_search_filter(ALICE, Action.READ)
    assert isinstance(reduced, AttrFilter)
    assert reduced.attribute == "id"
    assert isinstance(reduced.filter, InFilter)
    assert set(reduced.filter.values) == set(ids)
    # Create is unscoped by default (a new row has no id yet).
    assert (await policy.to_search_filter(ALICE, Action.CREATE)) == AllFilter()
    # An empty ACL denies.
    empty = await Acl(ids=frozenset()).to_search_filter(ALICE, Action.READ)
    assert isinstance(empty, NoMatchFilter)


async def test_materialized_acl_enumerates_and_caps(maker: Any) -> None:
    """The store enumerates the (capped) ACL ids and refuses an over-cap set."""
    await _add_user(maker, ALICE)
    async with maker() as session:
        session.add(
            ResourceAcl(id=uuid4(), principal_id=ALICE, resource_name="rbac-notes", resource_id="a")
        )
        session.add(
            ResourceAcl(id=uuid4(), principal_id=ALICE, resource_name="rbac-notes", resource_id="b")
        )
        await session.commit()
    store = SqlRbacStore(maker)
    assert sorted(await store.acl_ids(ALICE, "rbac-notes")) == ["a", "b"]
    resolver = RbacPolicyResolver(store=store)
    acl = await resolver.materialized_acl(Principal(id=ALICE, kind="user"), "rbac-notes")
    assert acl is not None
    assert acl.ids == frozenset({"a", "b"})
    # The join flavour is a subquery, not an enumerated list.
    assert await resolver.materialized_acl(None, "rbac-notes") is None

    # Over-cap enumeration is refused with an actionable error.
    async with maker() as session:
        for i in range(ACL_MAX_IDS + 1):
            session.add(
                ResourceAcl(
                    id=uuid4(),
                    principal_id=BOB,
                    resource_name="rbac-notes",
                    resource_id=f"x{i}",
                )
            )
        await session.commit()
    with pytest.raises(ResourceyConfigError, match="over the"):
        await store.acl_ids(BOB, "rbac-notes")


# ---------------------------------------------------------------------------
# Credential-threshold freshness bound
# ---------------------------------------------------------------------------


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_cache_ttl_bounds_membership_change(maker: Any) -> None:
    """A membership change is seen only after ``cache_ttl`` lapses (or invalidate)."""
    await _add_user(maker, ALICE)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    clock = _Clock()
    resolver = RbacPolicyResolver(
        store=SqlRbacStore(maker), cache_ttl=timedelta(seconds=60), clock=clock
    )
    principal = Principal(id=ALICE, kind="user")
    assert await resolver.resolve(resource, principal) == []
    # Grant ADMINS after the first resolution: still cached (empty).
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="admin",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    assert await resolver.resolve(resource, principal) == []
    # Past the threshold the change is honoured.
    clock.now += 61
    policies = await resolver.resolve(resource, principal)
    assert len(policies) == 1 and isinstance(policies[0], AllowAll)


async def test_invalidate_drops_cached_resolution(maker: Any) -> None:
    await _add_user(maker, ALICE)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = RbacPolicyResolver(
        store=SqlRbacStore(maker), cache_ttl=timedelta(seconds=60), clock=_Clock()
    )
    principal = Principal(id=ALICE, kind="user")
    await resolver.resolve(resource, principal)
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="admin",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    resolver.invalidate(principal_id=ALICE)
    assert len(await resolver.resolve(resource, principal)) == 1


async def test_no_ttl_resolves_every_request(maker: Any) -> None:
    """With no ``cache_ttl`` a change is seen immediately (the API-key posture)."""
    await _add_user(maker, ALICE)
    resource = SqlResource(Note, session_factory=maker, path="rbac-notes")
    resolver = RbacPolicyResolver(store=SqlRbacStore(maker))
    principal = Principal(id=ALICE, kind="user")
    assert await resolver.resolve(resource, principal) == []
    await _grant(
        maker,
        user_id=ALICE,
        group_id=ADMINS,
        role_name="admin",
        resource="rbac-notes",
        permission=AllowAll(),
    )
    assert len(await resolver.resolve(resource, principal)) == 1


# ---------------------------------------------------------------------------
# The RBAC resource set is exposed over the ordinary surface
# ---------------------------------------------------------------------------


async def test_rbac_resources_are_servable(maker: Any) -> None:
    """The RBAC models are ordinary ``SqlResource``s, addressable at their DTO names."""
    resource = SqlResource(User, session_factory=maker, path="users")
    async with await resource.get_service({}) as service:
        created = await service.create(resource.get_dto_type()(email="a@x", username="a"))
        assert created.username == "a"


def test_rbac_resources_are_exported() -> None:
    """The module exposes the full RBAC resource set builder."""
    from resourcey.v2.auth.auth_rbac import RBAC_MODELS, rbac_resources

    names = {model.__tablename__ for model in RBAC_MODELS}
    assert names == {
        "users",
        "groups",
        "group_users",
        "roles",
        "group_roles",
        "role_permissions",
        "resource_acls",
    }
    assert len(rbac_resources(session_factory=object())) == len(RBAC_MODELS)


def test_policy_from_rows_skips_corrupt_rows() -> None:
    """A corrupt / unknown policy row is skipped, never crashing or over-denying."""
    from resourcey.v2.auth.auth_rbac_store import policy_from_rows

    assert policy_from_rows([{"kind": "Nope"}, "not-a-dict"]) == []  # type: ignore[list-item]
    assert len(policy_from_rows([{"kind": "AllowAll"}])) == 1
