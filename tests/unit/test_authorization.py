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

import enum
from collections.abc import AsyncIterator, MutableMapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, PrivateAttr, SecretStr
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
    Acl,
    AllowAll,
    DenyAll,
    GroupMember,
    Owner,
    Policy,
    PolicyResolver,
    ReadOnly,
    bind_member_groups,
)
from resourcey.core.errors import ResourceyConfigError
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
    SearchFilter,
    and_,
    attr,
)


class AuthzBase(DeclarativeBase):
    pass


class Item(AuthzBase):
    __tablename__ = "authz_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(100))


class LabelPolicy(Policy):
    """An app-defined policy that scopes rows to a fixed ``label``.

    Defined here, outside ``src/``, it is the fixture for the custom-``Policy``
    contract (issue #191): discovery, round-trip, nesting, and the safe
    ``scopes_to_caller`` default all hold for a policy the framework never saw.

    Its reduction is a narrowing ``AttrFilter`` (not ``All`` / ``NoMatch``), which
    is the case that proves the policy filter is folded into the write's own
    condition rather than merely checked before it.
    """

    label: str

    async def to_search_filter(self, user_id: Any, action: Action) -> SearchFilter[Any]:
        if action is Action.CREATE:
            return AllFilter()
        return attr("label", EqFilter(value=self.label))


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
    for action in (Action.CREATE, Action.UPDATE, Action.DELETE):
        assert isinstance(await policy.to_search_filter(None, action), NoMatchFilter), action


# ---------------------------------------------------------------------------
# Custom actions — the policy stance (issue #187)
# ---------------------------------------------------------------------------


async def test_allow_all_grants_a_custom_action():
    assert isinstance(await AllowAll().to_search_filter(None, "use"), AllFilter)


async def test_deny_all_denies_a_custom_action():
    assert isinstance(await DenyAll().to_search_filter(None, "use"), NoMatchFilter)


async def test_read_only_denies_a_custom_action():
    # A custom action is not read-like; granting it would make ``ReadOnly``
    # grant more than its name promises.
    assert isinstance(await ReadOnly().to_search_filter(None, "use"), NoMatchFilter)


async def test_a_custom_action_is_not_normalized_away():
    # ``normalize_action`` maps only the derived standard members, so a custom
    # action reaches the policy as itself and can be granted independently.
    assert normalize_action("use") == "use"


class _UseAction(enum.StrEnum):
    """An app-declared action vocabulary — a StrEnum is its own strings."""

    USE = "use"


class Recording(Policy):
    """Records the action it is asked about — a policy that inspects its input."""

    _seen: list[Any] = PrivateAttr(default_factory=list)

    async def to_search_filter(self, user_id: Any, action: Any) -> SearchFilter[Any]:
        self._seen.append(action)
        return AllFilter()


async def test_a_policy_receives_the_custom_action_name_it_was_given():
    recording = Recording()
    # A plain string and an app-declared StrEnum member both arrive intact, and
    # both are the string ``"use"`` so a policy's ``==`` comparison works.
    assert isinstance(await recording.to_search_filter(None, "use"), AllFilter)
    assert isinstance(await recording.to_search_filter(None, _UseAction.USE), AllFilter)
    assert recording._seen == ["use", "use"]
    assert _UseAction.USE == "use"


def test_normalize_action_reduces_the_derived_members():
    assert normalize_action(Action.COUNT) is Action.SEARCH
    assert normalize_action(Action.BATCH_READ) is Action.READ
    for action in (Action.CREATE, Action.READ, Action.UPDATE, Action.DELETE, Action.SEARCH):
        assert normalize_action(action) is action


def test_policies_are_discriminated_and_round_trip():
    for policy in (AllowAll(), DenyAll(), ReadOnly()):
        dumped = policy.model_dump()
        assert dumped["kind"] == type(policy).__name__
        assert isinstance(Policy.model_validate(dumped), type(policy))


# ---------------------------------------------------------------------------
# Custom policies are a supported extension point (issue #191)
# ---------------------------------------------------------------------------


def test_an_app_defined_policy_is_discovered_and_round_trips():
    policy = LabelPolicy(label="mine")
    dumped = policy.model_dump()
    assert dumped == {"group_ids": [], "label": "mine", "kind": "LabelPolicy"}
    # ``dict(...)``: validating must not depend on the caller's dict surviving intact.
    restored = Policy.model_validate(dict(dumped))
    assert isinstance(restored, LabelPolicy)
    assert restored == policy
    assert dumped == {"group_ids": [], "label": "mine", "kind": "LabelPolicy"}


def test_an_app_defined_policy_nests_inside_a_built_in():
    nested = GroupMember(on_match=LabelPolicy(label="mine"), on_mismatch=DenyAll())
    restored = Policy.model_validate(dict(nested.model_dump()))
    assert isinstance(restored, GroupMember)
    assert isinstance(restored.on_match, LabelPolicy)
    assert restored.on_match.label == "mine"
    assert isinstance(restored.on_mismatch, DenyAll)


async def test_a_nested_app_defined_policy_reduces_through_its_parent():
    nested = GroupMember(on_match=LabelPolicy(label="mine"), group_ids=[uuid4()])
    member = bind_member_groups(nested, frozenset(nested.group_ids))
    reduced = await member.to_search_filter(None, Action.READ)
    assert reduced == attr("label", EqFilter(value="mine"))
    assert isinstance(await nested.to_search_filter(None, Action.READ), NoMatchFilter)


def test_a_policy_is_caller_scoped_unless_it_says_otherwise():
    assert LabelPolicy.scopes_to_caller is True
    assert LabelPolicy(label="mine").scopes_to_caller is True
    assert AllowAll.scopes_to_caller is False


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
        edits = await service.apply_edits(
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
        assert await service.apply_edits(
            [Update(item=_dto(resource)(id=seeded.id, label="z"))]
        ) == [None]


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
        # A batch update is denied (no write); the position is None.
        assert await service.apply_edits(
            [Update(item=_dto(resource)(id=seeded.id, label="z"))]
        ) == [None]


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


# ---------------------------------------------------------------------------
# Custom actions — enforcement (issue #187)
# ---------------------------------------------------------------------------


def _authorized_custom(
    inner: Any, policy: Policy, resource: SqlResource[Any, Any], *custom: str
) -> AuthorizedService[Any, Any]:
    return AuthorizedService(
        inner,
        policies=[policy],
        id_field=resource.get_id_field(),
        resource_name="items",
        custom_actions=frozenset(custom),
    )


async def test_authorize_permits_a_declared_custom_action_on_an_in_scope_row(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized_custom(
        await resource.get_service({}), AllowAll(), resource, "use"
    ) as service:
        assert (await service.authorize("use", seeded.id)).label == "a"


async def test_authorize_hides_an_out_of_scope_row_as_not_found(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized_custom(
        await resource.get_service({}), DenyAll(), resource, "use"
    ) as service:
        # 404, not 403: a non-permitted id must not leak its existence.
        with pytest.raises(NotFoundError):
            await service.authorize("use", seeded.id)


async def test_authorize_hides_an_absent_row_as_not_found(sql_resource):
    _maker, resource = sql_resource
    async with _authorized_custom(
        await resource.get_service({}), AllowAll(), resource, "use"
    ) as service:
        with pytest.raises(NotFoundError):
            await service.authorize("use", 999)


async def test_authorize_rejects_an_undeclared_custom_action(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized_custom(
        await resource.get_service({}), AllowAll(), resource, "use"
    ) as service:
        with pytest.raises(ResourceyConfigError, match="does not declare"):
            await service.authorize("consume", seeded.id)


async def test_authorize_refuses_every_name_when_none_are_declared(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        with pytest.raises(ResourceyConfigError, match="does not declare"):
            await service.authorize("use", seeded.id)


async def test_a_custom_action_is_independent_of_the_standard_ones(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    # ``ReadOnly`` grants read and denies the custom action, so the same row is
    # readable yet not usable — the distinction the issue is about.
    async with _authorized_custom(
        await resource.get_service({}), ReadOnly(), resource, "use"
    ) as service:
        assert (await service.read(seeded.id)).label == "a"
        with pytest.raises(NotFoundError):
            await service.authorize("use", seeded.id)


async def test_holding_read_does_not_imply_a_custom_action(sql_resource):
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    # ``Owner`` with no user id owns nothing: read is denied too, which proves
    # ``authorize`` goes through the policy rather than assuming read access.
    async with _authorized_custom(
        await resource.get_service({}), DenyAll(), resource, "use"
    ) as service:
        with pytest.raises(NotFoundError):
            await service.read(seeded.id)
        with pytest.raises(NotFoundError):
            await service.authorize("use", seeded.id)


async def test_owner_policy_scopes_a_custom_action_to_owned_rows():
    user_id = uuid4()
    custom = await Owner().to_search_filter(user_id, "use")
    # A custom action is not create, so it scopes like every other row action:
    # the caller may use exactly the rows they own.
    assert custom == await Owner().to_search_filter(user_id, Action.UPDATE)
    assert custom == attr("user_id", EqFilter(value=user_id))


async def test_acl_policy_uses_the_same_id_set_for_a_custom_action():
    listed, unlisted = uuid4(), uuid4()
    acl = Acl(ids=frozenset({listed}))
    assert await acl.to_search_filter(None, "use") == await acl.to_search_filter(None, Action.READ)
    assert (await acl.to_search_filter(None, "use")).matches(SimpleNamespace(id=listed))
    assert not (await acl.to_search_filter(None, "use")).matches(SimpleNamespace(id=unlisted))


async def test_group_member_policy_scopes_a_custom_action_by_membership():
    group = uuid4()
    policy = GroupMember(group_ids=frozenset({group}), on_match=AllowAll(), on_mismatch=DenyAll())
    # Membership is pre-bound by the resolver; the reduction is then the
    # matching sub-policy, exactly as for a standard action.
    assert isinstance(await policy.to_search_filter(uuid4(), "use"), NoMatchFilter)
    bound = bind_member_groups(policy, frozenset({group}))
    assert isinstance(await bound.to_search_filter(uuid4(), "use"), AllFilter)


async def test_read_only_denies_a_custom_action_while_granting_read():
    user_id = uuid4()
    assert isinstance(await ReadOnly().to_search_filter(user_id, "use"), NoMatchFilter)
    assert isinstance(await ReadOnly().to_search_filter(user_id, Action.READ), AllFilter)
    # A read-like *derived* action is still granted, so the denial is specific
    # to the custom name rather than to "anything not READ".
    assert isinstance(await ReadOnly().to_search_filter(user_id, Action.BATCH_READ), AllFilter)


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


async def test_apply_edits_with_an_absent_or_idless_target_is_none(sql_resource):
    _maker, resource = sql_resource
    async with _authorized(await resource.get_service({}), AllowAll(), resource) as service:
        edits = await service.apply_edits(
            [
                Update(item=_dto(resource)(id=999, label="x")),  # absent -> None
                Update(item=_dto(resource)(label="y")),  # no id -> None
            ]
        )
        assert edits == [None, None]


async def test_update_folds_a_row_scoping_policy_into_the_write(sql_resource):
    """A narrowing policy makes an out-of-scope update miss (404), in scope applies."""
    _maker, resource = sql_resource
    mine, theirs = await _seed(resource, "mine", "theirs")
    async with _authorized(
        await resource.get_service({}), LabelPolicy(label="mine"), resource
    ) as service:
        # In scope: the policy filter holds, so the write applies.
        updated = await service.update(_dto(resource)(id=mine.id, label="mine!"))
        assert updated.label == "mine!"
        # Out of scope: the policy filter fails the condition, so it is a 404
        # (indistinguishable from an absent row) and the row is untouched.
        with pytest.raises(NotFoundError):
            await service.update(_dto(resource)(id=theirs.id, label="hacked"))
    async with await resource.get_service({}) as raw:
        assert (await raw.read(theirs.id)).label == "theirs"


async def test_delete_folds_a_row_scoping_policy_into_the_write(sql_resource):
    _maker, resource = sql_resource
    mine, theirs = await _seed(resource, "mine", "theirs")
    async with _authorized(
        await resource.get_service({}), LabelPolicy(label="mine"), resource
    ) as service:
        with pytest.raises(NotFoundError):
            await service.delete(theirs.id)
        assert await service.delete(mine.id) is True
    async with await resource.get_service({}) as raw:
        assert [i.label for i in (await raw.search(limit=10)).items] == ["theirs"]


async def test_update_combines_the_policy_filter_with_the_caller_condition(sql_resource):
    """Both the policy scope and the caller's condition must hold for a write."""
    _maker, resource = sql_resource
    (mine,) = await _seed(resource, "mine")
    async with _authorized(
        await resource.get_service({}), LabelPolicy(label="mine"), resource
    ) as service:
        # Caller condition holds and policy holds -> applies.
        updated = await service.update(
            _dto(resource)(id=mine.id, label="mine!"),
            condition=attr("label", EqFilter(value="mine")),
        )
        assert updated.label == "mine!"
        # Caller condition fails (though the policy would hold) -> 404.
        with pytest.raises(NotFoundError):
            await service.update(
                _dto(resource)(id=mine.id, label="mine?"),
                condition=attr("label", EqFilter(value="nope")),
            )
    async with await resource.get_service({}) as raw:
        assert (await raw.read(mine.id)).label == "mine!"


async def test_apply_edits_folds_the_policy_into_each_update(sql_resource):
    _maker, resource = sql_resource
    mine, theirs = await _seed(resource, "mine", "theirs")
    async with _authorized(
        await resource.get_service({}), LabelPolicy(label="mine"), resource
    ) as service:
        results = await service.apply_edits(
            [
                Update(item=_dto(resource)(id=mine.id, label="mine!")),
                Update(item=_dto(resource)(id=theirs.id, label="hacked")),
            ]
        )
        assert results[0] is not None and results[0].label == "mine!"
        assert results[1] is None
    async with await resource.get_service({}) as raw:
        assert (await raw.read(theirs.id)).label == "theirs"


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


async def test_the_builder_threads_declared_custom_actions_into_the_service():
    """A hand-written route can check a custom action through the builder.

    This is the documented use case: the framework generates no route for a
    custom action, so the app adds one, resolves the service through the same
    builder (which is what applies the policy), and calls ``authorize``. The
    declared custom actions must survive that wrapping — otherwise every call
    would fail as undeclared.
    """
    from fastapi import Depends

    class Usable(SqlResource[Any, Any]):
        def get_custom_actions(self) -> frozenset[str]:
            return frozenset({"use"})

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(AuthzBase.metadata.create_all)
    items = Usable(Item, session_factory=maker, path="items")
    builder, key_view = _config_builder(AllowAll())
    manifest = Manifest(resources=[key_view, items])
    app = create_app(manifest, dependency_builder=builder)

    async def use_item(
        item_id: int,
        service: Any = Depends(builder.get_service_dependency(items)),  # noqa: B008
    ):
        return {"label": (await service.authorize("use", item_id)).label}

    app.post("/items/{item_id}/use")(use_item)

    async with manifest, await _client(app) as client:
        headers = {API_KEY_HEADER_NAME: "secret-one"}
        created = await client.post("/items", json={"label": "a"}, headers=headers)
        item_id = created.json()["id"]
        assert (await client.post(f"/items/{item_id}/use", headers=headers)).json() == {
            "label": "a"
        }
        # The custom action hides an absent row exactly like ``read`` does.
        assert (await client.post("/items/999/use", headers=headers)).status_code == 404
    await engine.dispose()


# ---------------------------------------------------------------------------
# The batch write gates each kind on its own action (issue #179)
# ---------------------------------------------------------------------------


class _UpdateOnlyUpdateDeniesCreate(Policy):
    """May update and delete, but never create — the split a single write gate hides."""

    async def to_search_filter(self, user_id: Any, action: Action) -> Any:
        from resourcey.auth.auth_policy import AllFilter, NoMatchFilter

        if action is Action.CREATE:
            return NoMatchFilter()
        return AllFilter()


async def test_a_batch_create_is_gated_on_create_not_on_a_shared_write_action(sql_resource):
    """A ``Create`` node asks ``Action.CREATE``; a policy denying create refuses it.

    Gating the create on a single write action (``batch_edit``, which reduced to
    ``UPDATE``) let a caller who may update but not create insert rows through a
    batch. The singular ``create()`` was correctly refused, so the two paths
    disagreed — a privilege escalation.
    """
    _maker, resource = sql_resource
    (seeded,) = await _seed(resource, "a")
    policy = _UpdateOnlyUpdateDeniesCreate()
    async with _authorized(await resource.get_service({}), policy, resource) as service:
        # The singular path refuses the create.
        with pytest.raises(ForbiddenError):
            await service.create(_dto(resource)(label="singular"))
        # ...and so must the batch path: a refused node writes nothing.
        results = await service.apply_edits([Create(item=_dto(resource)(label="batched"))])
        assert results == [None]
        # Nothing was inserted; the seeded row is untouched and the update works.
        assert await service.count() == 1
        assert (await service.update(_dto(resource)(id=seeded.id, label="b"))).label == "b"


async def test_a_batch_create_is_gated_on_create_over_the_default_route(authz_app):
    """The same gate holds through ``POST /batch-edit``, which is mounted by default."""
    from resourcey.auth.auth_policy import Acl

    app, manifest = authz_app(Acl(ids=frozenset({1}), on_create=DenyAll()))
    async with manifest, await _client(app) as client:
        headers = {API_KEY_HEADER_NAME: "secret-one"}
        # The singular route refuses the create.
        assert (
            await client.post("/items", json={"label": "single"}, headers=headers)
        ).status_code == 403
        # The cross-resource route must refuse it too, and write nothing.
        cross = await client.post(
            "/batch-edit",
            json=[{"resource": "items", "kind": "Create", "item": {"label": "cross"}}],
            headers=headers,
        )
        assert cross.status_code == 200
        assert [item["result"] for item in cross.json()] == [None]
        assert (await client.get("/items", headers=headers)).json()["items"] == []
