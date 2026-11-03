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

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, model_validator
from sqlalchemy import String, Uuid, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth import auth_rbac_store
from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME, ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_policy import (
    Acl,
    AllowAll,
    DenyAll,
    GroupMember,
    Owner,
    Policy,
    ReadOnly,
)
from resourcey.auth.auth_principal import Principal
from resourcey.auth.auth_rbac import (
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
from resourcey.auth.auth_rbac_resolver import RbacPolicyResolver
from resourcey.auth.auth_rbac_store import ACL_MAX_IDS, SqlRbacStore, policy_from_rows
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.http.app import create_app
from resourcey.sql.sql_resource import SqlResource
from resourcey.util.search_filter import AllFilter, AttrFilter, InFilter, NoMatchFilter

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


class ExplodingPolicy(Policy):
    """A loaded policy whose own validation code raises a non-``ValidationError``."""

    async def to_search_filter(self, user_id: UUID | None, action: Action) -> Any:
        raise NotImplementedError

    @model_validator(mode="after")
    def _explode(self) -> ExplodingPolicy:
        raise TypeError("policy bug, not a bad row")


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


@pytest.fixture
def reset_warning_log() -> Iterator[None]:
    """Isolate the process-wide once-per-signature log guard per test."""
    saved = set(auth_rbac_store._warned)
    auth_rbac_store._warned.clear()
    yield
    auth_rbac_store._warned.clear()
    auth_rbac_store._warned.update(saved)


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


async def test_store_warns_on_unknown_kind_and_keeps_the_known_grant(
    maker: Any, caplog: pytest.LogCaptureFixture, reset_warning_log: None
) -> None:
    """A row whose kind no class claims warns (naming the resource) and is skipped."""
    await _add_user(maker, ALICE, READERS)
    role_id = uuid4()
    async with maker() as session:
        session.add(Role(id=role_id, name="mixed"))
        session.add(GroupRole(id=uuid4(), group_id=READERS, role_id=role_id))
        session.add(
            RolePermission(
                id=uuid4(),
                role_id=role_id,
                resource="rbac-notes",
                permission={"kind": "NotImportedPolicy", "x": 1},
            )
        )
        session.add(
            RolePermission(
                id=uuid4(),
                role_id=role_id,
                resource="rbac-notes",
                permission=policy_to_json(AllowAll()),
            )
        )
        await session.commit()

    with caplog.at_level(logging.WARNING, logger="resourcey.auth.auth_rbac_store"):
        policies = await SqlRbacStore(maker).policies_for(ALICE, "rbac-notes")

    # Fail closed on the unknown row, keep the known grant, and say why.
    assert [type(policy) for policy in policies] == [AllowAll]
    assert "NotImportedPolicy" in caplog.text
    assert "'rbac-notes'" in caplog.text


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
    from resourcey.auth.auth_rbac import RBAC_MODELS, rbac_resources

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


def test_rbac_resource_paths_match_served_resources() -> None:
    """``rbac_resource_paths`` names exactly what ``rbac_resources`` serves.

    A role grant on the RBAC tables is keyed by path, so the two must not drift.
    """
    from resourcey.auth.auth_rbac import rbac_resource_paths, rbac_resources

    served = tuple(r.get_resource_path() for r in rbac_resources(session_factory=object()))
    assert rbac_resource_paths() == served


def test_policy_from_rows_skips_corrupt_rows(
    caplog: pytest.LogCaptureFixture, reset_warning_log: None
) -> None:
    """A corrupt / unknown policy row is skipped and reported, never over-denying."""
    with caplog.at_level(logging.WARNING, logger="resourcey.auth.auth_rbac_store"):
        assert (
            policy_from_rows(
                [{"kind": "Nope"}, "not-a-dict"],  # type: ignore[list-item]
                resource="messages",
            )
            == []
        )
    # Fail closed, but loudly: the unknown kind and the bad shape are both named.
    assert "Nope" in caplog.text
    assert "messages" in caplog.text
    assert "got str" in caplog.text
    assert len(policy_from_rows([{"kind": "AllowAll"}], resource="messages")) == 1


def test_policy_from_rows_warns_once_per_unknown_kind(
    caplog: pytest.LogCaptureFixture, reset_warning_log: None
) -> None:
    """A per-request resolution warns once per kind, not once per request."""
    with caplog.at_level(logging.WARNING, logger="resourcey.auth.auth_rbac_store"):
        for _ in range(5):
            policy_from_rows([{"kind": "NeverImported", "x": 1}], resource="messages")
    assert len(caplog.records) == 1
    assert "NeverImported" in caplog.text
    assert "messages" in caplog.text


def test_policy_from_rows_logs_each_unknown_kind_separately(
    caplog: pytest.LogCaptureFixture, reset_warning_log: None
) -> None:
    """The guard is per kind, so a second missing module is not hidden by the first."""
    with caplog.at_level(logging.WARNING, logger="resourcey.auth.auth_rbac_store"):
        policy_from_rows([{"kind": "MissingA"}, {"kind": "MissingB"}])
    assert len(caplog.records) == 2
    assert "MissingA" in caplog.text and "MissingB" in caplog.text


def test_policy_from_rows_skips_malformed_rows(
    caplog: pytest.LogCaptureFixture, reset_warning_log: None
) -> None:
    """A loaded kind with a corrupt field is skipped, warned about, and fails closed."""
    with caplog.at_level(logging.WARNING, logger="resourcey.auth.auth_rbac_store"):
        policies = policy_from_rows(
            [{"kind": "AllowAll", "group_ids": "not-a-list"}, {"kind": "AllowAll"}],
            resource="messages",
        )
    assert len(policies) == 1
    assert "malformed" in caplog.text
    assert "group_ids" in caplog.text


def test_policy_from_rows_does_not_swallow_programming_errors() -> None:
    """An error raised by a policy's own code is not mistaken for corrupt data."""
    with pytest.raises(TypeError, match="policy bug"):
        policy_from_rows([{"kind": "ExplodingPolicy"}])


def test_permission_column_is_jsonb_on_postgres() -> None:
    """``SELECT DISTINCT permission`` needs an equality operator: ``jsonb``, not ``json``.

    Plain PostgreSQL ``json`` has no ``=``, so the resolver's de-duplication query
    fails at runtime (SQLite tolerates it, so the in-memory suite would not catch
    it). Pin the dialect type so the portable ``JSON`` cannot regress Postgres.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    column = RolePermission.__table__.c.permission
    assert column.type.compile(dialect=postgresql.dialect()) == "JSONB"
    assert column.type.compile(dialect=sqlite.dialect()) == "JSON"
