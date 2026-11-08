"""A custom filter node enforced by a custom service (issue #191).

The second rung of "beyond column predicates": a predicate no backend can express
(here, a question for an external judgement service). The policy *returns* an
app-defined node; a service wrapping the real ``SqlService`` removes the node
from the filter, pushes the rest down, and applies the node itself.

Everything below runs over a real in-memory SQL table and the real
``AuthorizedService``. ``JudgementService`` is a stand-in for the external system
only — it is the boundary the node exists to reach, not a mock of framework code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_authorized_service import AuthorizedService
from resourcey.auth.auth_policy import Policy
from resourcey.core.errors import UnsupportedFilterError
from resourcey.core.manifest import Manifest
from resourcey.core.service import Action, Page, Service, Update
from resourcey.http.app import create_app
from resourcey.sql.sql_resource import SqlResource
from resourcey.sql.sql_service import SqlService
from resourcey.util.search_filter import (
    AllFilter,
    AndFilter,
    AttrFilter,
    BaseObjectFilter,
    EqFilter,
    NotFilter,
    OrFilter,
    SearchFilter,
    and_,
    attr,
    not_,
    or_,
)
from resourcey.util.sort_order import SortOrder


class SoulBase(DeclarativeBase):
    pass


class Soul(SoulBase):
    __tablename__ = "souls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    realm: Mapped[str] = mapped_column(String(20))


class JudgementUnavailableError(RuntimeError):
    """The external service could not be reached."""


class JudgementService:
    """Stand-in for the external system: answers one question per soul."""

    def __init__(self) -> None:
        self.damned: set[int] = set()
        self.down = False
        self.asked: list[int] = []

    def is_damned(self, soul_id: int) -> bool:
        if self.down:
            raise JudgementUnavailableError("judgement is unreachable")
        self.asked.append(soul_id)
        return soul_id in self.damned


JUDGEMENT = JudgementService()


class DamnedSoulFilter(SearchFilter[Any]):
    """Rows the external judgement service has condemned. Carries no data."""

    def matches(self, value: Any) -> bool:
        return JUDGEMENT.is_damned(value.id)


class Damned(Policy):
    """Admits only damned souls — a policy that returns the custom node."""

    async def to_search_filter(self, user_id: Any, action: Action | str) -> SearchFilter[Any]:
        return AllFilter() if action == Action.CREATE else DamnedSoulFilter()


def split_node(search_filter: SearchFilter[Any] | None) -> tuple[bool, SearchFilter[Any]]:
    """Separate a top-level ``DamnedSoulFilter`` conjunct from the rest.

    Only a top-level AND conjunct can be split off: under an ``OR`` / ``NOT`` it
    would have to be evaluated per row alongside pushed-down predicates, so the
    node there is refused rather than guessed at.
    """
    conjuncts = (
        search_filter.filters
        if isinstance(search_filter, AndFilter)
        else (() if search_filter is None else (search_filter,))
    )
    rest = [c for c in conjuncts if not isinstance(c, DamnedSoulFilter)]
    if any(_contains_node(c) for c in rest):
        raise UnsupportedFilterError("DamnedSoulFilter may only be a top-level AND conjunct")
    return len(rest) < len(conjuncts), and_(*rest)


def _contains_node(node: SearchFilter[Any]) -> bool:
    if isinstance(node, DamnedSoulFilter):
        return True
    if isinstance(node, AndFilter | OrFilter):
        return any(_contains_node(child) for child in node.filters)
    if isinstance(node, NotFilter | AttrFilter):
        return _contains_node(node.filter)
    return False


class SoulService(Service[Any, int]):
    """Wraps the SQL service; enforces ``DamnedSoulFilter`` in Python."""

    def __init__(self, inner: Service[Any, int], max_rounds: int = 50) -> None:
        super().__init__()
        self._inner = inner
        self._max_rounds = max_rounds

    async def __aenter__(self) -> SoulService:
        await super().__aenter__()
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)
        await super().__aexit__(*exc)

    async def create(self, payload: Any) -> Any:
        return await self._inner.create(payload)

    async def read(self, id: int) -> Any:  # noqa: A002
        return await self._inner.read(id)

    # A write whose condition carries the node is refused by the inner service,
    # which has no conversion for it. Do not intercept it here.
    async def update(self, payload: Any, *, condition: SearchFilter[Any] | None = None) -> Any:
        return await self._inner.update(payload, condition=condition)

    async def delete(self, id: int, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        return await self._inner.delete(id, condition=condition)

    async def batch_read(self, ids: list[int]) -> list[Any]:
        return await self._inner.batch_read(ids)

    async def apply_edits(self, edits: list[Any]) -> list[Any]:
        return await self._inner.apply_edits(edits)

    async def count(self, search_filter: SearchFilter[Any] | None = None) -> int:
        has_node, rest = split_node(search_filter)
        if has_node:
            raise UnsupportedFilterError("count() cannot apply DamnedSoulFilter without a scan")
        return await self._inner.count(rest)

    async def search(
        self,
        search_filter: SearchFilter[Any] | None = None,
        sort_order: SortOrder[Any] | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Any]:
        has_node, rest = split_node(search_filter)
        if not has_node:
            return await self._inner.search(rest, sort_order, cursor, limit)
        found: list[Any] = []
        for _ in range(self._max_rounds):
            page = await self._inner.search(rest, sort_order, cursor, limit - len(found))
            found.extend(row for row in page.items if DamnedSoulFilter().matches(row))
            cursor = page.next_cursor
            if len(found) >= limit or cursor is None:
                break
        return Page(items=found, limit=limit, next_cursor=cursor)


class SoulResource(SqlResource[Soul, int]):
    def make_service(
        self, ctx: MutableMapping[Any, Any], session_factory: async_sessionmaker[AsyncSession]
    ) -> Service[Any, int]:
        return SoulService(super().make_service(ctx, session_factory))


class PlainSoulResource(SqlResource[Soul, int]):
    """The inner resource on its own: ``allow_filter_iteration`` stays off."""


class IteratingSoulResource(SqlResource[Soul, int]):
    """The hazard: the in-memory fallback is on."""

    allow_filter_iteration = True


SOUL_COUNT = 20


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(SoulBase.metadata.create_all)
    async with session_maker() as session:
        realms = ("hell", "limbo")
        await session.execute(
            Soul.__table__.insert(),
            [{"id": i, "realm": realms[i % 2]} for i in range(1, SOUL_COUNT + 1)],
        )
        await session.commit()
    JUDGEMENT.damned = set()
    JUDGEMENT.down = False
    JUDGEMENT.asked = []
    yield session_maker
    await engine.dispose()


async def _service(resource: SqlResource[Soul, int]) -> Service[Any, int]:
    return await resource.get_service({})


def _authorized(inner: Service[Any, int], policy: Policy | None = None) -> AuthorizedService:
    return AuthorizedService(
        inner, policies=policy or Damned(), id_field="id", resource_name="souls"
    )


@pytest_asyncio.fixture
async def souls(maker) -> AsyncIterator[SoulResource]:
    resource = SoulResource(Soul, session_factory=maker)
    async with Manifest([resource]):
        yield resource


@pytest_asyncio.fixture
async def plain(maker) -> AsyncIterator[PlainSoulResource]:
    resource = PlainSoulResource(Soul, session_factory=maker)
    async with Manifest([resource]):
        yield resource


async def _drain(service: Service[Any, int], limit: int, **kwargs: Any) -> list[list[int]]:
    """Every page's ids, following ``next_cursor`` to the end."""
    pages: list[list[int]] = []
    cursor = None
    while True:
        page = await service.search(cursor=cursor, limit=limit, **kwargs)
        pages.append([item.id for item in page.items])
        cursor = page.next_cursor
        if cursor is None:
            return pages


class TestSearch:
    async def test_returns_only_matching_rows(self, souls) -> None:
        JUDGEMENT.damned = {3, 8, 15}
        async with _authorized(await _service(souls)) as service:
            page = await service.search(limit=10)
        assert [item.id for item in page.items] == [3, 8, 15]

    async def test_fills_the_page_across_inner_rounds_and_follows_the_cursor(self, souls) -> None:
        JUDGEMENT.damned = {2, 5, 8, 11, 14, 17}
        async with _authorized(await _service(souls)) as service:
            pages = await _drain(service, limit=4)
        assert pages == [[2, 5, 8, 11], [14, 17]]

    async def test_a_round_asks_only_for_the_shortfall(self, souls) -> None:
        """Asking for a full page each round would overshoot ``limit`` and skip rows."""
        JUDGEMENT.damned = {1, 2, 4, 5, 6}
        async with _authorized(await _service(souls)) as service:
            pages = await _drain(service, limit=3)
        assert pages == [[1, 2, 4], [5, 6]]

    async def test_every_row_is_examined_exactly_once(self, souls) -> None:
        """A round never overshoots, so no row is skipped or judged twice."""
        JUDGEMENT.damned = {1, 7, 20}
        async with _authorized(await _service(souls)) as service:
            await _drain(service, limit=2)
        assert JUDGEMENT.asked == list(range(1, SOUL_COUNT + 1))

    async def test_pushes_the_rest_of_the_filter_down(self, souls) -> None:
        JUDGEMENT.damned = set(range(1, SOUL_COUNT + 1))
        async with _authorized(await _service(souls)) as service:
            page = await service.search(attr("realm", EqFilter(value="hell")), limit=50)
        assert all(item.realm == "hell" for item in page.items)
        # Only rows that survived the SQL predicate reach the external service.
        assert set(JUDGEMENT.asked) == {item.id for item in page.items}
        assert len(page.items) == SOUL_COUNT // 2

    async def test_a_short_page_still_carries_a_cursor_when_the_round_cap_is_hit(
        self, maker
    ) -> None:
        JUDGEMENT.damned = {SOUL_COUNT}
        resource = SoulResource(Soul, session_factory=maker)
        async with Manifest([resource]):
            inner = SoulService(SqlService(resource, {}, maker), max_rounds=2)
            async with inner:
                first = await inner.search(DamnedSoulFilter(), limit=3)
                assert first.items == []
                assert first.next_cursor is not None
                pages = [[], *await _drain_from(inner, first.next_cursor, limit=3)]
        assert pages[-1] == [SOUL_COUNT]

    async def test_no_node_means_no_filtering(self, souls) -> None:
        async with await _service(souls) as service:
            page = await service.search(limit=5)
        assert [item.id for item in page.items] == [1, 2, 3, 4, 5]
        assert JUDGEMENT.asked == []


async def _drain_from(service: Service[Any, int], cursor: str, limit: int) -> list[list[int]]:
    pages: list[list[int]] = []
    while True:
        page = await service.search(DamnedSoulFilter(), cursor=cursor, limit=limit)
        pages.append([item.id for item in page.items])
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


class TestFailsClosed:
    async def test_a_conditional_write_carrying_the_node_is_refused(self, souls) -> None:
        async with await _service(souls) as service:
            dto = souls.get_dto_type()(id=1, realm="changed")
            with pytest.raises(UnsupportedFilterError):
                await service.update(dto, condition=DamnedSoulFilter())
            with pytest.raises(UnsupportedFilterError):
                await service.delete(1, condition=DamnedSoulFilter())
            assert (await service.read(1)).realm == "limbo"
        assert JUDGEMENT.asked == []

    async def test_a_batch_edit_carrying_the_node_is_refused(self, souls) -> None:
        async with await _service(souls) as service:
            dto = souls.get_dto_type()(id=1, realm="changed")
            with pytest.raises(UnsupportedFilterError):
                await service.apply_edits([Update(item=dto, condition=DamnedSoulFilter())])

    async def test_a_policy_node_on_a_write_is_refused_through_authorized_service(
        self, souls
    ) -> None:
        async with _authorized(await _service(souls)) as service:
            dto = souls.get_dto_type()(id=1, realm="changed")
            with pytest.raises(UnsupportedFilterError):
                await service.update(dto)
            with pytest.raises(UnsupportedFilterError):
                await service.delete(1)

    async def test_the_node_reaching_the_inner_service_unsplit_is_refused(self, plain) -> None:
        async with await _service(plain) as service:
            with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
                await service.search(DamnedSoulFilter(), limit=5)
            with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
                await service.count(DamnedSoulFilter())
        assert JUDGEMENT.asked == []

    async def test_allow_filter_iteration_would_call_the_check_once_per_row(self, maker) -> None:
        """The hazard the docs warn about: on, a leaked node scans the whole table."""
        JUDGEMENT.damned = {4}
        resource = IteratingSoulResource(Soul, session_factory=maker)
        async with Manifest([resource]), await _service(resource) as service:
            page = await service.search(DamnedSoulFilter(), limit=5)
        assert [item.id for item in page.items] == [4]
        assert len(JUDGEMENT.asked) == SOUL_COUNT

    async def test_an_iterating_inner_still_refuses_a_write_carrying_the_node(self, maker) -> None:
        resource = IteratingSoulResource(Soul, session_factory=maker)
        async with Manifest([resource]), await _service(resource) as service:
            with pytest.raises(UnsupportedFilterError):
                await service.delete(1, condition=DamnedSoulFilter())

    async def test_count_with_the_node_raises_rather_than_scanning(self, souls) -> None:
        async with _authorized(await _service(souls)) as service:
            with pytest.raises(UnsupportedFilterError, match="count"):
                await service.count()
        assert JUDGEMENT.asked == []

    async def test_count_without_the_node_is_delegated(self, souls) -> None:
        async with await _service(souls) as service:
            assert await service.count(attr("realm", EqFilter(value="hell"))) == SOUL_COUNT // 2


class TestErrorsPropagate:
    async def test_a_failing_check_propagates_from_search(self, souls) -> None:
        JUDGEMENT.down = True
        async with _authorized(await _service(souls)) as service:
            with pytest.raises(JudgementUnavailableError):
                await service.search(limit=5)

    async def test_a_failing_check_propagates_from_read(self, souls) -> None:
        JUDGEMENT.down = True
        async with _authorized(await _service(souls)) as service:
            with pytest.raises(JudgementUnavailableError):
                await service.read(1)

    async def test_a_failing_check_propagates_from_batch_read(self, souls) -> None:
        JUDGEMENT.down = True
        async with _authorized(await _service(souls)) as service:
            with pytest.raises(JudgementUnavailableError):
                await service.batch_read([1, 2])

    async def test_a_failing_check_is_not_inverted_by_not(self, souls) -> None:
        """``NotFilter`` around a ``False`` would be ``True`` — fail-open — if it were swallowed."""
        JUDGEMENT.down = True
        dto = souls.get_dto_type()(id=1, realm="hell")
        with pytest.raises(JudgementUnavailableError):
            not_(DamnedSoulFilter()).matches(dto)

    async def test_a_failing_check_under_a_not_policy_propagates_from_read(self, souls) -> None:
        class NotDamned(Policy):
            async def to_search_filter(self, user_id: Any, action: Action | str) -> Any:
                return not_(DamnedSoulFilter())

        JUDGEMENT.down = True
        async with _authorized(await _service(souls), NotDamned()) as service:
            with pytest.raises(JudgementUnavailableError):
                await service.read(1)


class TestUnsplittableShapes:
    @pytest.mark.parametrize(
        "build",
        [
            lambda: or_(DamnedSoulFilter(), attr("realm", EqFilter(value="hell"))),
            lambda: not_(DamnedSoulFilter()),
            lambda: and_(
                attr("realm", EqFilter(value="hell")),
                or_(DamnedSoulFilter(), attr("realm", EqFilter(value="limbo"))),
            ),
        ],
        ids=["or", "not", "nested-or"],
    )
    async def test_a_node_under_or_or_not_raises(self, souls, build) -> None:
        async with await _service(souls) as service:
            with pytest.raises(UnsupportedFilterError, match="top-level AND"):
                await service.search(build(), limit=5)
            with pytest.raises(UnsupportedFilterError, match="top-level AND"):
                await service.count(build())
        assert JUDGEMENT.asked == []

    async def test_a_policy_that_ors_the_node_with_another_predicate_raises(self, souls) -> None:
        class DamnedOrHell(Policy):
            async def to_search_filter(self, user_id: Any, action: Action | str) -> Any:
                return or_(DamnedSoulFilter(), attr("realm", EqFilter(value="hell")))

        async with _authorized(await _service(souls), DamnedOrHell()) as service:
            with pytest.raises(UnsupportedFilterError, match="top-level AND"):
                await service.search(limit=5)


class TestWireExposure:
    """A wire condition resolves any registered ``SearchFilter`` by ``kind``."""

    def test_a_wire_condition_can_name_the_custom_node(self) -> None:
        edit = Update.model_validate(
            {"item": {"id": 1, "realm": "x"}, "condition": {"kind": "DamnedSoulFilter"}}
        )
        assert isinstance(edit.condition, DamnedSoulFilter)

    async def test_a_batch_edit_naming_the_node_is_refused_before_any_check(self, maker) -> None:
        resource = SoulResource(Soul, session_factory=maker)
        manifest: Manifest = Manifest(resources=[resource])
        async with manifest:
            transport = ASGITransport(app=create_app(manifest))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/batch-edit",
                    json=[
                        {
                            "resource": "souls",
                            "kind": "Update",
                            "item": {"id": 1, "realm": "changed"},
                            "condition": {"kind": "DamnedSoulFilter"},
                        }
                    ],
                )
        assert response.status_code == 501
        assert JUDGEMENT.asked == []


class DamnedQuery(BaseObjectFilter[Any]):
    """A declared object filter whose lowering emits the custom node."""

    is_damned__eq: bool | None = None

    def _build_standard_filter(self) -> SearchFilter[Any]:
        return DamnedSoulFilter() if self.is_damned__eq else AllFilter()


class QueriedSoulResource(SoulResource):
    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        return DamnedQuery


class TestQuerySurface:
    """A declared filter class names the query *surface*; it does not lower the request."""

    async def test_the_lowering_of_a_declared_filter_is_not_applied_to_a_query(self, maker) -> None:
        JUDGEMENT.damned = {2}
        resource = QueriedSoulResource(Soul, session_factory=maker)
        manifest: Manifest = Manifest(resources=[resource])
        async with manifest:
            transport = ASGITransport(app=create_app(manifest))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/souls", params={"is_damned__eq": "true"})
        assert response.status_code == 501
        assert JUDGEMENT.asked == []
