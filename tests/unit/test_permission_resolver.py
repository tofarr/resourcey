"""Tests for the UserPermission model, PermissionResolver, and CreatorPermission integration (issue #4)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.auth.auth_models import AuthBase, User, UserPermission
from resourcey.auth.permission import (
    CreatorPermission,
    Denied,
    Permission,
    Permitted,
    ReadOnly,
)
from resourcey.auth.permission_resolver import (
    DefaultPermissions,
    PermissionResolver,
)
from resourcey.auth.secured_service import SecuredService
from resourcey.resource.errors import ForbiddenError, NotFoundError
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.search_filter import (
    ALL,
    AllSearchFilter,
    NoneSearchFilter,
    SearchFilter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeService(BaseService):
    """In-memory service for SecuredService tests."""

    def __init__(self, items: dict[Any, Any] | None = None) -> None:
        self._items: dict[Any, Any] = items or {}
        self.created: list[Any] = []
        self.deleted: list[Any] = []

    async def create(self, payload: Any) -> Any:
        self.created.append(payload)
        item_id = getattr(payload, "id", None) or uuid.uuid4()
        if not hasattr(payload, "id"):
            object.__setattr__(payload, "id", item_id)
        self._items[item_id] = payload
        return payload

    async def read(self, id: Any) -> Any:  # noqa: A002
        return self._items.get(id)

    async def update(self, id: Any, payload: Any) -> Any:  # noqa: A002
        self._items[id] = payload
        return payload

    async def delete(self, id: Any) -> None:  # noqa: A002
        self.deleted.append(id)
        self._items.pop(id, None)

    async def search(self, **kwargs: Any) -> list[Any]:
        filters = kwargs.get("filters")
        items = list(self._items.values())
        if filters is None:
            return items
        return [item for item in items if filters.matches(item)]

    async def count(self, **kwargs: Any) -> int:
        filters = kwargs.get("filters")
        if filters is None:
            return len(self._items)
        return sum(1 for item in self._items.values() if filters.matches(item))

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        return [self._items.get(i) for i in ids]

    async def batch_edit(self, edits: list[tuple[Any, Any]]) -> list[Any]:
        results = []
        for edit_id, payload in edits:
            self._items[edit_id] = payload
            results.append(payload)
        return results

    def compute_cache_header(self, items: list[Any]) -> Any:
        return None

    def compute_count_cache_header(self, count: int, filters: Any) -> Any:
        return None


def make_item(item_id: Any = None, creator_id: Any = None) -> SimpleNamespace:
    return SimpleNamespace(id=item_id or uuid.uuid4(), creator_id=creator_id)


def make_secured(
    inner: BaseService,
    resource_type: str,
    user_id: uuid.UUID | None,
    resolver_filter: SearchFilter[Any] | None,
) -> SecuredService:
    """Build a SecuredService with a stub resolver returning a fixed filter."""

    async def _resolver(
        rt: str,
        act: Action,
        uid: uuid.UUID | None,
        groups: frozenset[uuid.UUID],
    ) -> SearchFilter[Any] | None:
        return resolver_filter

    return SecuredService(
        inner=inner,
        resource_type=resource_type,
        resource_name=resource_type,
        user_id=user_id,
        groups=frozenset(),
        resolver=_resolver,
    )


# ---------------------------------------------------------------------------
# UserPermission model
# ---------------------------------------------------------------------------


class TestUserPermissionModel:
    def test_permission_round_trip(self) -> None:
        """A Permission serialized to dict and back preserves its kind."""
        policy = CreatorPermission(on_match=Permitted(), on_mismatch=ReadOnly())
        raw = policy.model_dump(mode="json")
        restored = Permission.model_validate(raw)
        assert isinstance(restored, CreatorPermission)
        assert isinstance(restored.on_match, Permitted)
        assert isinstance(restored.on_mismatch, ReadOnly)

    def test_permission_json_column_serializable(self) -> None:
        """The policy dict is JSON-serializable for the JSON column."""
        policy = AclPermission(item_ids=[uuid.uuid4()], on_match=Permitted())
        raw = policy.model_dump(mode="json")
        # Must be JSON-serializable (the JSON column stores it as JSON).
        serialized = json.dumps(raw)
        deserialized = json.loads(serialized)
        restored = Permission.model_validate(deserialized)
        assert isinstance(restored, AclPermission)


# ---------------------------------------------------------------------------
# Import AclPermission for the test above
# ---------------------------------------------------------------------------
from resourcey.auth.permission import AclPermission  # noqa: E402

# ---------------------------------------------------------------------------
# DefaultPermissions
# ---------------------------------------------------------------------------


class TestDefaultPermissions:
    def test_empty_defaults(self) -> None:
        dp = DefaultPermissions()
        assert dp.for_resource("anything") == []

    def test_add_and_retrieve(self) -> None:
        dp = DefaultPermissions()
        dp2 = dp.add("document", Permitted())
        assert dp.for_resource("document") == []  # original unchanged
        assert len(dp2.for_resource("document")) == 1
        assert isinstance(dp2.for_resource("document")[0], Permitted)

    def test_from_config_valid(self) -> None:
        raw = {
            "document": [{"kind": "Permitted"}],
            "public": [{"kind": "ReadOnly"}],
        }
        dp = DefaultPermissions.from_config(raw)
        docs = dp.for_resource("document")
        assert len(docs) == 1
        assert isinstance(docs[0], Permitted)
        pubs = dp.for_resource("public")
        assert len(pubs) == 1
        assert isinstance(pubs[0], ReadOnly)

    def test_from_config_skips_invalid(self) -> None:
        raw = {
            "document": [{"kind": "Permitted"}, {"kind": "nonexistent"}],
            "bad": [{"not_a": "valid_permission"}],
        }
        dp = DefaultPermissions.from_config(raw)
        assert len(dp.for_resource("document")) == 1
        assert dp.for_resource("bad") == []

    def test_from_config_empty(self) -> None:
        dp = DefaultPermissions.from_config({})
        assert dp.for_resource("anything") == []


# ---------------------------------------------------------------------------
# PermissionResolver (defaults-only mode, no DB)
# ---------------------------------------------------------------------------


class TestPermissionResolverDefaults:
    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return uuid.uuid4()

    @pytest.mark.asyncio
    async def test_no_policies_returns_none(self, user_id: uuid.UUID) -> None:
        resolver = PermissionResolver(None)
        result = await resolver.resolve("document", Action.READ, user_id)
        assert result is None  # fail-closed

    @pytest.mark.asyncio
    async def test_default_permitted(self, user_id: uuid.UUID) -> None:
        defaults = DefaultPermissions({"document": [Permitted()]})
        resolver = PermissionResolver(None, defaults=defaults)
        result = await resolver.resolve("document", Action.READ, user_id)
        assert result is not None
        assert isinstance(result, AllSearchFilter)

    @pytest.mark.asyncio
    async def test_default_denied(self, user_id: uuid.UUID) -> None:
        defaults = DefaultPermissions({"document": [Denied()]})
        resolver = PermissionResolver(None, defaults=defaults)
        result = await resolver.resolve("document", Action.READ, user_id)
        # Denied reduces to NONE, but there IS a policy, so result is NONE not None.
        assert result is not None
        assert isinstance(result, NoneSearchFilter)

    @pytest.mark.asyncio
    async def test_default_read_only(self, user_id: uuid.UUID) -> None:
        defaults = DefaultPermissions({"document": [ReadOnly()]})
        resolver = PermissionResolver(None, defaults=defaults)
        read_result = await resolver.resolve("document", Action.READ, user_id)
        assert isinstance(read_result, AllSearchFilter)
        create_result = await resolver.resolve("document", Action.CREATE, user_id)
        assert isinstance(create_result, NoneSearchFilter)

    @pytest.mark.asyncio
    async def test_default_creator(self, user_id: uuid.UUID) -> None:
        defaults = DefaultPermissions({"document": [CreatorPermission(on_match=Permitted())]})
        resolver = PermissionResolver(None, defaults=defaults)
        read_result = await resolver.resolve("document", Action.READ, user_id)
        # CreatorPermission with on_match=Permitted reduces to a filter that
        # matches own items. For READ it's the CreatorMatchFilter scope.
        assert read_result is not None
        assert not isinstance(read_result, NoneSearchFilter)
        assert not isinstance(read_result, AllSearchFilter)

    @pytest.mark.asyncio
    async def test_anonymous_with_default_permitted(self) -> None:
        defaults = DefaultPermissions({"public": [Permitted()]})
        resolver = PermissionResolver(None, defaults=defaults)
        result = await resolver.resolve("public", Action.READ, None)
        assert isinstance(result, AllSearchFilter)

    @pytest.mark.asyncio
    async def test_anonymous_no_policies_returns_none(self) -> None:
        resolver = PermissionResolver(None)
        result = await resolver.resolve("anything", Action.READ, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_union_of_defaults(self, user_id: uuid.UUID) -> None:
        defaults = DefaultPermissions(
            {
                "doc": [Denied(), Permitted()],
            }
        )
        resolver = PermissionResolver(None, defaults=defaults)
        result = await resolver.resolve("doc", Action.READ, user_id)
        # OR(Denied, Permitted) = Permitted = All
        assert isinstance(result, AllSearchFilter)


# ---------------------------------------------------------------------------
# PermissionResolver with DB (integration)
# ---------------------------------------------------------------------------


class TestPermissionResolverWithDb:
    @pytest.fixture
    async def engine(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def session(self, engine):
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return uuid.uuid4()

    @pytest.fixture
    async def user_with_permissions(self, session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
        # Create a user row (needed for FK constraint).
        user = User(
            id=user_id,
            email="test@example.com",
            username="testuser",
            enabled=True,
        )
        session.add(user)
        await session.flush()

        # Add a UserPermission: Permitted on "document".
        perm = UserPermission(
            user_id=user_id,
            resource_type="document",
            permission=Permitted().model_dump(mode="json"),
        )
        session.add(perm)
        await session.flush()
        return user_id

    @pytest.mark.asyncio
    async def test_db_permitted(
        self,
        session: AsyncSession,
        user_with_permissions: uuid.UUID,
    ) -> None:
        resolver = PermissionResolver(session)
        result = await resolver.resolve("document", Action.READ, user_with_permissions)
        assert isinstance(result, AllSearchFilter)

    @pytest.mark.asyncio
    async def test_db_no_permissions_returns_none(
        self,
        session: AsyncSession,
        user_with_permissions: uuid.UUID,
    ) -> None:
        resolver = PermissionResolver(session)
        result = await resolver.resolve("nonexistent", Action.READ, user_with_permissions)
        assert result is None

    @pytest.mark.asyncio
    async def test_db_creator_permission(
        self,
        session: AsyncSession,
        user_with_permissions: uuid.UUID,
    ) -> None:
        # Add a CreatorPermission for "task" resource.
        perm = UserPermission(
            user_id=user_with_permissions,
            resource_type="task",
            permission=CreatorPermission(on_match=Permitted()).model_dump(mode="json"),
        )
        session.add(perm)
        await session.flush()

        resolver = PermissionResolver(session)
        result = await resolver.resolve("task", Action.READ, user_with_permissions)
        assert result is not None
        assert not isinstance(result, AllSearchFilter)
        assert not isinstance(result, NoneSearchFilter)

    @pytest.mark.asyncio
    async def test_db_and_defaults_combined(
        self,
        session: AsyncSession,
        user_with_permissions: uuid.UUID,
    ) -> None:
        # DB has Permitted for "document"; defaults have ReadOnly for "document".
        defaults = DefaultPermissions({"document": [ReadOnly()]})
        resolver = PermissionResolver(session, defaults=defaults)
        # READ: OR(Permitted, ReadOnly) = All (both grant read).
        result = await resolver.resolve("document", Action.READ, user_with_permissions)
        assert isinstance(result, AllSearchFilter)
        # CREATE: OR(Permitted, ReadOnly) = All (Permitted grants create, ReadOnly denies).
        result = await resolver.resolve("document", Action.CREATE, user_with_permissions)
        assert isinstance(result, AllSearchFilter)

    @pytest.mark.asyncio
    async def test_db_corrupt_policy_skipped(
        self,
        session: AsyncSession,
        user_with_permissions: uuid.UUID,
    ) -> None:
        # Insert a corrupt permission dict.
        bad = UserPermission(
            user_id=user_with_permissions,
            resource_type="bad_resource",
            permission={"kind": "nonexistent_policy"},
        )
        session.add(bad)
        await session.flush()

        resolver = PermissionResolver(session)
        result = await resolver.resolve("bad_resource", Action.READ, user_with_permissions)
        # Corrupt policy is skipped, no other policies -> None (fail-closed).
        assert result is None


# ---------------------------------------------------------------------------
# CreatorPermission end-to-end with SecuredService
# ---------------------------------------------------------------------------


class TestCreatorPermissionIntegration:
    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return uuid.uuid4()

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return uuid.uuid4()

    @pytest.fixture
    def items(self, user_id: uuid.UUID, other_user_id: uuid.UUID) -> dict[Any, Any]:
        own = make_item(uuid.uuid4(), creator_id=user_id)
        other = make_item(uuid.uuid4(), creator_id=other_user_id)
        unowned = make_item(uuid.uuid4(), creator_id=None)
        return {own.id: own, other.id: other, unowned.id: unowned}

    async def test_creator_can_read_own(
        self,
        user_id: uuid.UUID,
        items: dict[Any, Any],
    ) -> None:
        inner = FakeService(dict(items))
        policy = CreatorPermission(on_match=Permitted())
        filt = policy.to_search_filter(user_id, Action.READ)
        secured = make_secured(inner, "task", user_id, filt)

        own_id = next(i for i, v in items.items() if v.creator_id == user_id)
        result = await secured.read(own_id)
        assert result is not None

    async def test_creator_cannot_read_others(
        self,
        user_id: uuid.UUID,
        items: dict[Any, Any],
    ) -> None:
        inner = FakeService(dict(items))
        policy = CreatorPermission(on_match=Permitted())
        filt = policy.to_search_filter(user_id, Action.READ)
        secured = make_secured(inner, "task", user_id, filt)

        other_id = next(
            i for i, v in items.items() if v.creator_id is not None and v.creator_id != user_id
        )
        with pytest.raises(NotFoundError):
            await secured.read(other_id)

    async def test_creator_search_filters_to_own(
        self,
        user_id: uuid.UUID,
        items: dict[Any, Any],
    ) -> None:
        inner = FakeService(dict(items))
        policy = CreatorPermission(on_match=Permitted())
        filt = policy.to_search_filter(user_id, Action.SEARCH)
        secured = make_secured(inner, "task", user_id, filt)

        results = await secured.search()
        assert len(results) == 1
        assert results[0].creator_id == user_id

    async def test_creator_create_stamps_creator_id(self, user_id: uuid.UUID) -> None:
        inner = FakeService()
        policy = CreatorPermission(on_create=Permitted())
        filt = policy.to_search_filter(user_id, Action.CREATE)
        secured = make_secured(inner, "task", user_id, filt)

        payload = SimpleNamespace(title="New Task", creator_id=None)
        result = await secured.create(payload)
        assert result is not None
        assert result.creator_id == user_id

    async def test_creator_create_denied(self, user_id: uuid.UUID) -> None:
        inner = FakeService()
        policy = CreatorPermission()
        filt = policy.to_search_filter(user_id, Action.CREATE)
        secured = make_secured(inner, "task", user_id, filt)

        payload = SimpleNamespace(title="New Task")
        with pytest.raises(ForbiddenError):
            await secured.create(payload)

    async def test_creator_read_only_on_own(
        self,
        user_id: uuid.UUID,
        items: dict[Any, Any],
    ) -> None:
        inner = FakeService(dict(items))
        policy = CreatorPermission(on_match=ReadOnly())
        filt = policy.to_search_filter(user_id, Action.READ)
        secured = make_secured(inner, "task", user_id, filt)

        own_id = next(i for i, v in items.items() if v.creator_id == user_id)
        result = await secured.read(own_id)
        assert result is not None

        filt_update = policy.to_search_filter(user_id, Action.UPDATE)
        secured_update = make_secured(inner, "task", user_id, filt_update)
        with pytest.raises(NotFoundError):
            await secured_update.update(own_id, SimpleNamespace(title="Updated"))


# ---------------------------------------------------------------------------
# SecuredService with async resolver (PermissionResolver integration)
# ---------------------------------------------------------------------------


class TestSecuredServiceWithAsyncResolver:
    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return uuid.uuid4()

    async def test_async_resolver_permitted(self, user_id: uuid.UUID) -> None:
        inner = FakeService({uuid.uuid4(): make_item(creator_id=user_id)})

        async def resolver(
            rt: str,
            act: Action,
            uid: uuid.UUID | None,
            groups: frozenset[uuid.UUID],
        ) -> SearchFilter[Any] | None:
            return ALL

        secured = SecuredService(
            inner=inner,
            resource_type="task",
            resource_name="Task",
            user_id=user_id,
            groups=frozenset(),
            resolver=resolver,
        )
        results = await secured.search()
        assert len(results) == 1

    async def test_async_resolver_returns_none(self, user_id: uuid.UUID) -> None:
        inner = FakeService({uuid.uuid4(): make_item(creator_id=user_id)})

        async def resolver(
            rt: str,
            act: Action,
            uid: uuid.UUID | None,
            groups: frozenset[uuid.UUID],
        ) -> SearchFilter[Any] | None:
            return None  # fail-closed

        secured = SecuredService(
            inner=inner,
            resource_type="task",
            resource_name="Task",
            user_id=user_id,
            groups=frozenset(),
            resolver=resolver,
        )
        results = await secured.search()
        assert len(results) == 0  # None -> NONE -> empty

    async def test_sync_resolver_still_works(self, user_id: uuid.UUID) -> None:
        inner = FakeService({uuid.uuid4(): make_item(creator_id=user_id)})

        def resolver(
            rt: str,
            act: Action,
            uid: uuid.UUID | None,
            groups: frozenset[uuid.UUID],
        ) -> SearchFilter[Any] | None:
            return ALL

        secured = SecuredService(
            inner=inner,
            resource_type="task",
            resource_name="Task",
            user_id=user_id,
            groups=frozenset(),
            resolver=resolver,
        )
        results = await secured.search()
        assert len(results) == 1
