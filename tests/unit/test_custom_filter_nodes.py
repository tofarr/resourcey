"""The custom-filter-node escape hatch (issue #195).

A ``SearchFilter`` names a column on the row being queried, so a predicate over
a *related* table — "messages sent to this user" — has no standard expression.
``register_logical`` is the supported extension point: the app defines the node
and its SQL, written as ``EXISTS (SELECT 1 FROM related WHERE ...)``.

What these tests pin:

* the ``EXISTS`` shape works end to end — search, count, and the conditional
  write path, where a ``JOIN`` could not be used at all;
* the node's SQL agrees with its ``matches`` on a fixture holding a NULL row and
  a multi-match row, including under negation;
* ``resolve(filter_)`` is the IO seam: a node that queries before it can build
  its criterion caches onto ``ctx.meta`` and its sync handler reads it back;
* a node that cannot be evaluated in memory raises from ``matches`` rather than
  returning a plausible-but-wrong answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Integer, String, exists, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.sql.filter_converter import (
    SqlFilterContext,
    SqlFilterConverter,
    register_logical,
)
from resourcey.sql.sql_resource import SqlResource
from resourcey.util.search_filter import (
    AllFilter,
    EqFilter,
    SearchFilter,
    and_,
    attr,
    not_,
    or_,
)

# ---------------------------------------------------------------------------
# Fixture schema: a message is "sent to" a user through a link table
# ---------------------------------------------------------------------------


class RelatedBase(DeclarativeBase):
    pass


class Message(RelatedBase):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MessageRecipient(RelatedBase):
    __tablename__ = "message_recipients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TenantProject(RelatedBase):
    __tablename__ = "tenant_projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# The custom nodes
# ---------------------------------------------------------------------------


class SentTo(SearchFilter[Sequence[int]]):
    """Messages delivered to ``user_id`` — a predicate over the link table.

    ``matches`` receives the recipient ids of one message (the in-memory
    equivalent of the link-table lookup), so the node has a reference semantics
    its SQL must agree with.
    """

    user_id: int

    def matches(self, value: Sequence[int]) -> bool:
        return self.user_id in value


def _sent_to_positive(converter: SqlFilterConverter, node: SearchFilter[Any]) -> Any:
    """``EXISTS (SELECT 1 FROM message_recipients WHERE ...)`` — never a JOIN.

    A join duplicates the message row per recipient, which breaks keyset paging
    and inflates ``count()``; ``update()`` / ``delete()`` expose no ``.join()`` at
    all, so a join-based criterion would fail on the write path.
    """
    recipients = MessageRecipient.__table__.c
    return exists().where(
        recipients.message_id == converter.ctx.columns["id"],
        recipients.user_id == node.user_id,  # type: ignore[attr-defined]
    )


def _sent_to_negated(converter: SqlFilterConverter, node: SearchFilter[Any]) -> Any:
    """The exact complement: ``NOT EXISTS (...)``.

    NULL-safe by construction — a message with no recipient row at all satisfies
    ``NOT EXISTS``, exactly as the in-memory complement does.
    """
    return ~_sent_to_positive(converter, node)


class TenantProjects(SearchFilter[int]):
    """A node whose criterion needs one query before it can be built.

    ``resolve`` reads the tenant's project ids and caches them; the sync handler
    then compiles ``sender_id IN (...)``. ``matches`` raises because this node is
    pushdown-only — the in-memory paths must fail loudly, not guess.
    """

    tenant_id: int

    def matches(self, value: int) -> bool:
        raise NotImplementedError("TenantProjects is pushdown-only; it has no in-memory semantics")

    async def resolve(self, ctx: SqlFilterContext) -> None:
        session = ctx.session
        assert session is not None, "a resolving node needs a session"
        result = await session.execute(
            select(TenantProject.__table__.c.project_id).where(
                TenantProject.__table__.c.tenant_id == self.tenant_id
            )
        )
        ctx.meta[self] = tuple(result.scalars().all())


def _tenant_projects_positive(converter: SqlFilterConverter, node: SearchFilter[Any]) -> Any:
    project_ids = converter.ctx.meta[node]
    if not project_ids:
        return false()  # fail-closed: a tenant with no projects matches nothing
    return Message.__table__.c.sender_id.in_(project_ids)


def _tenant_projects_negated(converter: SqlFilterConverter, node: SearchFilter[Any]) -> Any:
    project_ids = converter.ctx.meta[node]
    if not project_ids:
        return true()
    column = Message.__table__.c.sender_id
    return column.is_(None) | column.not_in(project_ids)


# Registration is a process-global mutation, so it happens once at import.
register_logical(SentTo, _sent_to_positive, _sent_to_negated)
register_logical(TenantProjects, _tenant_projects_positive, _tenant_projects_negated)


class Unregistered(SearchFilter[int]):
    """No SQL handler: it forces the iteration fallback."""

    value: int = 1

    def matches(self, value: int) -> bool:
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def related_env() -> AsyncIterator[tuple[Any, SqlResource[Any, Any]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    resource = SqlResource(Message, session_factory=maker)
    async with engine.begin() as conn:
        await conn.run_sync(RelatedBase.metadata.create_all)
    async with maker() as session:
        await session.execute(
            Message.__table__.insert(),
            [
                {"id": 1, "text": "a", "sender_id": 7},  # two recipients: 10, 11
                {"id": 2, "text": "b", "sender_id": 7},  # one recipient: 11
                {"id": 3, "text": None, "sender_id": None},  # no recipients at all
            ],
        )
        await session.execute(
            MessageRecipient.__table__.insert(),
            [
                {"id": 1, "message_id": 1, "user_id": 10},
                {"id": 2, "message_id": 1, "user_id": 11},
                {"id": 3, "message_id": 2, "user_id": 11},
                {"id": 4, "message_id": 2, "user_id": None},
            ],
        )
        await session.execute(
            TenantProject.__table__.insert(),
            [
                {"id": 1, "tenant_id": 5, "project_id": 7},
                {"id": 2, "tenant_id": 6, "project_id": 8},
            ],
        )
        await session.commit()
    yield maker, resource
    await engine.dispose()


async def _ids(
    resource: SqlResource[Any, Any], filter_: SearchFilter[Any], session: AsyncSession
) -> list[int]:
    converter = resource.build_filter_converter(session)
    standard = filter_.create_standard_filter()
    await converter.resolve(standard)
    stmt = converter.apply(select(Message.__table__.c.id), standard)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# End to end: the EXISTS shape
# ---------------------------------------------------------------------------


class TestExistsPushdown:
    async def test_positive_matches_only_delivered_messages(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            assert await _ids(resource, SentTo(user_id=10), session) == [1]
            assert await _ids(resource, SentTo(user_id=11), session) == [1, 2]
            # A user who received nothing matches nothing, and the message with
            # no recipient row at all is not dragged in.
            assert await _ids(resource, SentTo(user_id=99), session) == []

    async def test_negation_is_the_exact_complement(self, related_env) -> None:
        """The no-recipient row belongs to the negated branch, not to neither."""
        _maker, resource = related_env
        async with resource._session_factory() as session:
            assert await _ids(resource, not_(SentTo(user_id=10)), session) == [2, 3]
            assert await _ids(resource, not_(SentTo(user_id=99)), session) == [1, 2, 3]

    async def test_composes_with_standard_nodes(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            both = and_(SentTo(user_id=11), attr("text", EqFilter(value="a")))
            assert await _ids(resource, both, session) == [1]
            either = or_(SentTo(user_id=10), attr("text", EqFilter(value="b")))
            assert await _ids(resource, either, session) == [1, 2]

    async def test_search_and_count_agree(self, related_env) -> None:
        """``count()`` is ``select(func.count())`` — a JOIN would inflate it here."""
        _maker, resource = related_env
        async with await resource.get_service() as service:
            page = await service.search(search_filter=SentTo(user_id=11))
            assert [item.id for item in page.items] == [1, 2]
            assert await service.count(search_filter=SentTo(user_id=11)) == 2
            assert await service.count(search_filter=SentTo(user_id=10)) == 1
            assert await service.count() == 3

    async def test_conditional_write_honours_the_node(self, related_env) -> None:
        """``UPDATE ... WHERE id = 1 AND EXISTS(...)`` — a JOIN could not express this."""
        _maker, resource = related_env
        dto_type = resource.get_dto_type()
        async with await resource.get_service() as service:
            held = await service.update(
                dto_type(id=1, text="updated"), condition=SentTo(user_id=10)
            )
            assert held is not None and held.text == "updated"
            missed = await service.update(dto_type(id=2, text="nope"), condition=SentTo(user_id=10))
            assert missed is None
            assert (await service.read(2)).text == "b"  # unchanged
            assert await service.delete(2, condition=SentTo(user_id=10)) is False
            assert await service.delete(2, condition=SentTo(user_id=11)) is True


# ---------------------------------------------------------------------------
# The matches contract
# ---------------------------------------------------------------------------


class TestMatchesAgreement:
    """A custom node's SQL must agree with ``matches`` on every row.

    The fixture carries a NULL column and a multi-match row, mirroring the
    NULL-safe ``TestSqlConversion`` cases: the complement of ``EXISTS`` has to
    include the row no recipient references.
    """

    async def test_sql_agrees_with_matches_including_null_and_negation(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            rows = (await session.execute(select(Message.__table__))).mappings().all()
            links = (
                await session.execute(
                    select(
                        MessageRecipient.__table__.c.message_id,
                        MessageRecipient.__table__.c.user_id,
                    ).where(MessageRecipient.__table__.c.user_id.is_not(None))
                )
            ).all()
            recipients = {
                row.id: tuple(link.user_id for link in links if link.message_id == row.id)
                for row in rows
            }
            positive = await _ids(resource, SentTo(user_id=10), session)
            negated = await _ids(resource, not_(SentTo(user_id=10)), session)
        for row in rows:
            expected = SentTo(user_id=10).matches(recipients[row.id])
            assert (row.id in positive) == expected
            assert (row.id in negated) == (not expected)


class TestMatchesFailsLoudly:
    def test_pushdown_only_node_raises_from_matches(self) -> None:
        """A node that cannot be evaluated in memory must raise, not guess."""
        with pytest.raises(NotImplementedError, match="pushdown-only"):
            TenantProjects(tenant_id=5).matches(1)

    async def test_allow_filter_iteration_fails_loudly(self, related_env) -> None:
        """The in-memory fallback must not silently return the wrong rows.

        With ``allow_filter_iteration`` on, an unconvertible *other* node makes
        the service materialise ids and call ``matches`` — which raises here
        rather than the fallback pretending the node matched nothing.
        """
        _maker, resource = related_env
        resource.allow_filter_iteration = True
        try:
            async with await resource.get_service() as service:
                with pytest.raises(NotImplementedError, match="pushdown-only"):
                    await service.search(
                        search_filter=and_(Unregistered(), TenantProjects(tenant_id=5))
                    )
        finally:
            resource.allow_filter_iteration = False


# ---------------------------------------------------------------------------
# The resolve() seam: one query, then match
# ---------------------------------------------------------------------------


class TestResolveSeam:
    async def test_resolve_caches_one_query_then_handler_matches(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            # sender_id 7 is tenant 5's project, so messages 1 and 2 match.
            assert await _ids(resource, TenantProjects(tenant_id=5), session) == [1, 2]
            assert await _ids(resource, TenantProjects(tenant_id=6), session) == []
            # The complement is NULL-safe: the NULL sender (row 3) is included.
            assert await _ids(resource, not_(TenantProjects(tenant_id=5)), session) == [3]
            assert await _ids(resource, not_(TenantProjects(tenant_id=6)), session) == [1, 2, 3]

    async def test_a_tenant_with_no_projects_matches_nothing_fail_closed(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            assert await _ids(resource, TenantProjects(tenant_id=404), session) == []
            assert await _ids(resource, not_(TenantProjects(tenant_id=404)), session) == [
                1,
                2,
                3,
            ]

    async def test_resolve_runs_once_per_node_instance(self, related_env) -> None:
        """The cache is keyed by node, so a repeated node resolves once."""
        _maker, resource = related_env
        node = TenantProjects(tenant_id=5)
        async with resource._session_factory() as session:
            converter = resource.build_filter_converter(session)
            await converter.resolve(and_(node, node))
            assert list(converter.ctx.meta) == [node]

    async def test_standard_nodes_define_no_hook(self, related_env) -> None:
        _maker, resource = related_env
        async with resource._session_factory() as session:
            converter = resource.build_filter_converter(session)
            await converter.resolve(and_(AllFilter(), attr("id", EqFilter(value=1))))
            assert converter.ctx.meta == {}

    async def test_handler_without_resolution_raises(self, related_env) -> None:
        """Skipping ``resolve`` must raise, never fall through to a wrong answer."""
        _maker, resource = related_env
        async with resource._session_factory() as session:
            converter = resource.build_filter_converter(session)
            with pytest.raises(KeyError):
                converter.condition(TenantProjects(tenant_id=5))


# ---------------------------------------------------------------------------
# The registry is process-global, and that is documented
# ---------------------------------------------------------------------------


def test_registration_documents_its_global_scope() -> None:
    from resourcey.mongo import mongo_filter_converter
    from resourcey.sql import filter_converter

    for register in (
        filter_converter.register_logical,
        filter_converter.register_operator,
        mongo_filter_converter.register_logical,
        mongo_filter_converter.register_operator,
    ):
        doc = register.__doc__ or ""
        assert "process-global" in doc or "global-registry" in doc
