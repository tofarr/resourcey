"""Unit tests for the permission policy object and SecuredService wrapper."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from resourcey.auth.permission import (
    AclPermission,
    CreatorPermission,
    Denied,
    GroupPermission,
    Permission,
    Permitted,
    ReadOnly,
    normalize_action,
)
from resourcey.auth.secured_service import SecuredService
from resourcey.resource.errors import ForbiddenError, NotFoundError
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.search_filter import (
    ALL,
    NONE,
    AllSearchFilter,
    NoneSearchFilter,
    SearchFilter,
)

# ---------------------------------------------------------------------------
# Permission policy reductions
# ---------------------------------------------------------------------------


class TestPermissionReductions:
    def test_permitted_is_all(self) -> None:
        assert isinstance(Permitted().to_search_filter(None, Action.READ), AllSearchFilter)

    def test_denied_is_none(self) -> None:
        assert isinstance(Denied().to_search_filter(None, Action.READ), NoneSearchFilter)

    @pytest.mark.parametrize(
        ("action", "allowed"),
        [
            (Action.READ, True),
            (Action.SEARCH, True),
            (Action.COUNT, True),
            (Action.CREATE, False),
            (Action.UPDATE, False),
            (Action.DELETE, False),
            (Action.BATCH_EDIT, False),
        ],
    )
    def test_readonly(self, action: Action, allowed: bool) -> None:
        filt = ReadOnly().to_search_filter(None, action)
        assert isinstance(filt, AllSearchFilter) if allowed else isinstance(filt, NoneSearchFilter)

    def test_normalize_action(self) -> None:
        assert normalize_action(Action.COUNT) is Action.SEARCH
        assert normalize_action(Action.BATCH_READ) is Action.READ
        assert normalize_action(Action.BATCH_EDIT) is Action.UPDATE
        assert normalize_action(Action.CREATE) is Action.CREATE


class TestPermissionRoundTrip:
    @pytest.mark.parametrize(
        "policy",
        [
            Permitted(),
            Denied(),
            ReadOnly(),
            AclPermission(item_ids=[uuid.uuid4()], on_match=Permitted()),
            CreatorPermission(on_match=Permitted(), on_mismatch=ReadOnly()),
            GroupPermission(group_ids=[uuid.uuid4()], on_match=Permitted()),
        ],
    )
    def test_roundtrip(self, policy: Permission) -> None:
        dumped = policy.model_dump(mode="json")
        restored = Permission.model_validate(dumped)
        # The discriminator restores the concrete subclass; ``kind`` is a
        # computed field so a deep-equality of the dump is brittle across
        # nested defaults. The type identity + re-dump round-trip is the check.
        assert type(restored) is type(policy)
        assert Permission.model_validate(restored.model_dump(mode="json")).kind == restored.kind


class TestCreatorPermission:
    def test_anonymous_denied(self) -> None:
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(None, Action.READ)
        assert isinstance(filt, NoneSearchFilter)

    def test_own_items_match(self) -> None:
        user = uuid.uuid4()
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.READ)
        item = SimpleNamespace(id=1, creator_id=user)
        assert filt.matches(item)

    def test_other_items_denied(self) -> None:
        user = uuid.uuid4()
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.READ)
        item = SimpleNamespace(id=1, creator_id=uuid.uuid4())
        assert not filt.matches(item)

    def test_null_creator_denied(self) -> None:
        user = uuid.uuid4()
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.READ)
        item = SimpleNamespace(id=1, creator_id=None)
        assert not filt.matches(item)


class TestAclPermission:
    def test_in_list_permitted(self) -> None:
        target = uuid.uuid4()
        p = AclPermission(item_ids=[target], on_match=Permitted())
        filt = p.to_search_filter(uuid.uuid4(), Action.READ)
        assert filt.matches(SimpleNamespace(id=target))

    def test_out_of_list_denied(self) -> None:
        target = uuid.uuid4()
        p = AclPermission(item_ids=[target], on_match=Permitted())
        filt = p.to_search_filter(uuid.uuid4(), Action.READ)
        assert not filt.matches(SimpleNamespace(id=uuid.uuid4()))

    def test_empty_ids_uses_on_mismatch(self) -> None:
        p = AclPermission(on_mismatch=Permitted())
        filt = p.to_search_filter(uuid.uuid4(), Action.READ)
        assert filt.matches(SimpleNamespace(id=uuid.uuid4()))

    def test_too_many_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="at most 100"):
            AclPermission(item_ids=[uuid.uuid4() for _ in range(101)])


class TestGroupPermission:
    def test_member_gets_on_match(self) -> None:
        g = uuid.uuid4()
        p = GroupPermission(group_ids=[g], on_match=Permitted())
        filt = p.to_search_filter(uuid.uuid4(), Action.READ, groups=frozenset({g}))
        assert isinstance(filt, AllSearchFilter)

    def test_non_member_gets_on_mismatch(self) -> None:
        g = uuid.uuid4()
        p = GroupPermission(group_ids=[g], on_match=Permitted())
        filt = p.to_search_filter(uuid.uuid4(), Action.READ, groups=frozenset())
        assert isinstance(filt, NoneSearchFilter)


# ---------------------------------------------------------------------------
# SecuredService wrapper
# ---------------------------------------------------------------------------


class _FakeInner(BaseService):
    """A minimal in-memory service for wrapper tests.

    Stores items as :class:`SimpleNamespace` instances (attribute access),
    mirroring how real ORM rows expose ``creator_id`` etc. to the filters.
    """

    def __init__(self) -> None:
        self._items: dict[Any, SimpleNamespace] = {}
        self._next = 1

    async def create(self, payload: Any) -> Any:
        item = SimpleNamespace(id=self._next, **_payload_dict(payload))
        self._items[self._next] = item
        self._next += 1
        return item

    async def read(self, id: Any) -> Any:  # noqa: A002
        return self._items.get(id)

    async def update(self, id: Any, payload: Any) -> Any:  # noqa: A002
        item = self._items.get(id)
        if item is None:
            return None
        for k, v in _payload_dict(payload).items():
            setattr(item, k, v)
        return item

    async def delete(self, id: Any) -> None:  # noqa: A002
        self._items.pop(id, None)

    async def search(self, *, filters: SearchFilter[Any] | None = None, **kw: Any) -> Any:
        items = list(self._items.values())
        if filters is not None:
            items = [i for i in items if filters.matches(i)]
        return {"items": items, **kw}

    async def count(self, *, filters: SearchFilter[Any] | None = None) -> int:
        items = list(self._items.values())
        if filters is not None:
            items = [i for i in items if filters.matches(i)]
        return len(items)

    async def batch_read(self, ids: list[Any]) -> list[Any]:
        return [self._items.get(i) for i in ids]

    async def batch_edit(self, edits: list[tuple[Any, Any]]) -> list[Any]:
        results = []
        for edit_id, payload in edits:
            item = self._items.get(edit_id)
            if item is None:
                results.append(None)
                continue
            for k, v in _payload_dict(payload).items():
                setattr(item, k, v)
            results.append(item)
        return results


def _payload_dict(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if hasattr(payload, "__dict__"):
        return dict(payload.__dict__)
    return {}


def _make_svc(
    inner: BaseService,
    user_id: uuid.UUID | None,
    resolver_action_filter: dict[Action, SearchFilter[Any]],
) -> SecuredService:
    def resolver(
        rt: str, action: Action, uid: uuid.UUID | None, groups: frozenset[uuid.UUID]
    ) -> SearchFilter[Any] | None:
        return resolver_action_filter.get(action)

    return SecuredService(
        inner,
        resource_type="widget",
        resource_name="Widget",
        user_id=user_id,
        groups=frozenset(),
        resolver=resolver,
    )


class TestSecuredCreate:
    async def test_create_permitted(self) -> None:
        inner = _FakeInner()
        svc = _make_svc(inner, None, {Action.CREATE: ALL})
        item = await svc.create(SimpleNamespace(text="hi"))
        assert item.text == "hi"

    async def test_create_denied_raises_403(self) -> None:
        inner = _FakeInner()
        svc = _make_svc(inner, None, {Action.CREATE: NONE})
        with pytest.raises(ForbiddenError):
            await svc.create(SimpleNamespace(text="hi"))

    async def test_create_no_policy_denies(self) -> None:
        inner = _FakeInner()
        svc = _make_svc(inner, None, {})
        with pytest.raises(ForbiddenError):
            await svc.create(SimpleNamespace(text="hi"))

    async def test_create_stamps_creator_id(self) -> None:
        user = uuid.uuid4()
        inner = _FakeInner()
        svc = _make_svc(inner, user, {Action.CREATE: ALL})
        payload = SimpleNamespace(text="hi", creator_id=None)
        await svc.create(payload)
        assert payload.creator_id == user


class TestSecuredRead:
    async def test_read_permitted_item(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        item = await inner.create(SimpleNamespace(creator_id=user))
        svc = _make_svc(inner, user, {Action.READ: ALL})
        assert await svc.read(item.id) == item

    async def test_read_denied_item_raises_404(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        other = uuid.uuid4()
        item = await inner.create(SimpleNamespace(creator_id=other))
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.READ)
        svc = _make_svc(inner, user, {Action.READ: filt})
        with pytest.raises(NotFoundError):
            await svc.read(item.id)

    async def test_read_missing_returns_none(self) -> None:
        inner = _FakeInner()
        svc = _make_svc(inner, None, {Action.READ: ALL})
        assert await svc.read(999) is None


class TestSecuredUpdateDelete:
    async def test_update_permitted(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        item = await inner.create(SimpleNamespace(creator_id=user))
        svc = _make_svc(inner, user, {Action.UPDATE: ALL})
        result = await svc.update(item.id, SimpleNamespace(text="new"))
        assert result.text == "new"

    async def test_update_denied_raises_404(self) -> None:
        inner = _FakeInner()
        other = uuid.uuid4()
        item = await inner.create(SimpleNamespace(creator_id=other))
        user = uuid.uuid4()
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.UPDATE)
        svc = _make_svc(inner, user, {Action.UPDATE: filt})
        with pytest.raises(NotFoundError):
            await svc.update(item.id, SimpleNamespace(text="new"))

    async def test_delete_denied_raises_404(self) -> None:
        inner = _FakeInner()
        other = uuid.uuid4()
        item = await inner.create(SimpleNamespace(creator_id=other))
        user = uuid.uuid4()
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.DELETE)
        svc = _make_svc(inner, user, {Action.DELETE: filt})
        with pytest.raises(NotFoundError):
            await svc.delete(item.id)
        # item still present
        assert await inner.read(item.id) is not None


class TestSecuredSearchCount:
    async def test_search_filters_to_permission_scope(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        await inner.create(SimpleNamespace(creator_id=user, text="mine"))
        await inner.create(SimpleNamespace(creator_id=uuid.uuid4(), text="theirs"))
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.SEARCH)
        svc = _make_svc(inner, user, {Action.SEARCH: filt, Action.COUNT: filt})
        result = await svc.search()
        assert len(result["items"]) == 1
        assert result["items"][0].text == "mine"

    async def test_count_filters_to_permission_scope(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        await inner.create(SimpleNamespace(creator_id=user))
        await inner.create(SimpleNamespace(creator_id=uuid.uuid4()))
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.COUNT)
        svc = _make_svc(inner, user, {Action.COUNT: filt})
        assert await svc.count() == 1

    async def test_search_deny_yields_empty(self) -> None:
        inner = _FakeInner()
        await inner.create(SimpleNamespace(text="x"))
        svc = _make_svc(inner, None, {Action.SEARCH: NONE})
        result = await svc.search()
        assert result["items"] == []


class TestSecuredBatch:
    async def test_batch_read_nones_non_permitted(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        own = await inner.create(SimpleNamespace(creator_id=user))
        other = await inner.create(SimpleNamespace(creator_id=uuid.uuid4()))
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.BATCH_READ)
        svc = _make_svc(inner, user, {Action.BATCH_READ: filt})
        results = await svc.batch_read([own.id, other.id, 999])
        assert results[0] == own
        assert results[1] is None
        assert results[2] is None

    async def test_batch_edit_skips_non_permitted(self) -> None:
        inner = _FakeInner()
        user = uuid.uuid4()
        own = await inner.create(SimpleNamespace(creator_id=user, text="a"))
        other = await inner.create(SimpleNamespace(creator_id=uuid.uuid4(), text="b"))
        filt = CreatorPermission(on_match=Permitted()).to_search_filter(user, Action.BATCH_EDIT)
        svc = _make_svc(inner, user, {Action.BATCH_EDIT: filt})
        results = await svc.batch_edit(
            [(own.id, SimpleNamespace(text="x")), (other.id, SimpleNamespace(text="y"))]
        )
        assert results[0].text == "x"
        assert results[1] is None
        # other was not modified
        assert (await inner.read(other.id)).text == "b"
