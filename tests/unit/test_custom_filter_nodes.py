"""Custom filter nodes: the supported escape hatch (issue #195).

A :class:`SearchFilter` names a column on the row being queried, so a predicate
over a **related** table — "messages sent to this user" — had no expression.
The converter's rule list is the answer: an app defines a node, a rule for it,
and prepends the rule through ``SqlResource.get_filter_rules``.

These tests pin the three properties that make that path safe:

* a rule that needs a related table uses ``EXISTS``, not ``JOIN`` — a join
  duplicates rows and inflates ``count()``, and ``update`` / ``delete`` expose
  no ``.join()`` at all, so a join-based criterion would work for reads and fail
  for conditional writes;
* the rule's SQL agrees with its ``matches`` on every row, including NULLs and
  under negation;
* an unconvertible node **raises**, so nothing is ever silently skipped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import String, Uuid, exists, false, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.errors import UnsupportedFilterError
from resourcey.core.manifest import Manifest
from resourcey.sql.filter_converter import (
    DEFAULT_RULES,
    SqlFilterContext,
    SqlFilterConverter,
    SqlFilterRule,
    ValueRule,
)
from resourcey.sql.sql_resource import SqlResource
from resourcey.util.search_filter import (
    AllFilter,
    BaseObjectFilter,
    EqFilter,
    SearchFilter,
    and_,
    attr,
    not_,
)


class RecipientBase(DeclarativeBase):
    pass


class Message(RecipientBase):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    text: Mapped[str | None] = mapped_column(String(50), nullable=True)


class MessageRecipient(RecipientBase):
    __tablename__ = "message_recipients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[UUID] = mapped_column(Uuid)
    user_id: Mapped[int]


MESSAGE_IDS = [UUID(int=1), UUID(int=2), UUID(int=3)]
# Message 1 goes to 10 and 11 (a multi-match row), message 2 to 11 only, and
# message 3 to nobody (the row with no related row).
RECIPIENTS = [
    {"message_id": MESSAGE_IDS[0], "user_id": 10},
    {"message_id": MESSAGE_IDS[0], "user_id": 11},
    {"message_id": MESSAGE_IDS[1], "user_id": 11},
]


class SentTo(SearchFilter[Message]):
    """Messages sent to ``user_id`` — a predicate over the related table."""

    user_id: int

    def matches(self, value: Message) -> bool:
        raise NotImplementedError("pushdown only: this node has no in-memory predicate")


@dataclass(frozen=True)
class SentToRule(SqlFilterRule):
    """``SentTo`` as a correlated ``EXISTS``."""

    def handles(self, node: SearchFilter[Any]) -> bool:
        return isinstance(node, SentTo)

    async def convert(
        self,
        node: SearchFilter[Any],
        converter: SqlFilterConverter,
        *,
        column=None,
        negated: bool = False,
    ):
        recipients = MessageRecipient.__table__.c
        assert isinstance(node, SentTo)
        criterion = exists().where(
            recipients.message_id == converter.ctx.id_column,
            recipients.user_id == node.user_id,
        )
        return ~criterion if negated else criterion


class MessageResource(SqlResource[Message, UUID]):
    def get_filter_rules(self) -> tuple[SqlFilterRule, ...]:
        return (SentToRule(), *DEFAULT_RULES)


class PlainMessageResource(SqlResource[Message, UUID]):
    """A resource that did **not** register the custom rule."""


class MessageFilter(BaseObjectFilter[Message]):
    text__contains: str | None = None


@pytest_asyncio.fixture
async def messages() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(RecipientBase.metadata.create_all)
    async with maker() as session:
        await session.execute(
            Message.__table__.insert(),
            [
                {"id": MESSAGE_IDS[0], "text": "alpha"},
                {"id": MESSAGE_IDS[1], "text": "beta"},
                {"id": MESSAGE_IDS[2], "text": None},
            ],
        )
        await session.execute(MessageRecipient.__table__.insert(), RECIPIENTS)
        await session.commit()
    yield maker
    await engine.dispose()


async def _search(resource, search_filter) -> list[UUID]:
    async with await resource.get_service() as service:
        page = await service.search(search_filter=search_filter, limit=10)
        return [item.id for item in page.items]


async def _count(resource, search_filter) -> int:
    async with await resource.get_service() as service:
        return await service.count(search_filter=search_filter)


@pytest_asyncio.fixture
async def message_resource(messages) -> AsyncIterator[MessageResource]:
    resource = MessageResource(Message, session_factory=messages)
    async with Manifest([resource]):
        yield resource


@pytest_asyncio.fixture
async def plain_resource(messages) -> AsyncIterator[PlainMessageResource]:
    resource = PlainMessageResource(Message, session_factory=messages)
    async with Manifest([resource]):
        yield resource


class TestExistsPushdown:
    async def test_exists_selects_the_related_rows(self, message_resource) -> None:
        assert await _search(message_resource, SentTo(user_id=10)) == [MESSAGE_IDS[0]]
        assert await _search(message_resource, SentTo(user_id=11)) == MESSAGE_IDS[:2]

    async def test_exists_does_not_duplicate_a_multi_match_row(self, message_resource) -> None:
        """A JOIN would return message 1 twice and inflate the count; EXISTS does not."""
        assert await _count(message_resource, SentTo(user_id=11)) == 2

    async def test_not_exists_is_the_exact_complement(self, message_resource) -> None:
        assert await _search(message_resource, not_(SentTo(user_id=10))) == MESSAGE_IDS[1:]

    async def test_exists_composes_with_standard_nodes(self, message_resource) -> None:
        both = and_(SentTo(user_id=11), attr("text", EqFilter(value="alpha")))
        assert await _search(message_resource, both) == [MESSAGE_IDS[0]]

    async def test_exists_works_on_a_conditional_write(self, messages) -> None:
        """``update`` / ``delete`` expose no ``.join()``, so EXISTS is the only shape."""
        resource = MessageResource(Message, session_factory=messages)
        async with Manifest([resource]), await resource.get_service() as service:
            updated = await service.update(
                resource.get_dto_type()(id=MESSAGE_IDS[1], text="changed"),
                condition=SentTo(user_id=11),
            )
            assert updated is not None and updated.text == "changed"
            # A failing condition deletes nothing; the row stays.
            assert await service.delete(MESSAGE_IDS[2], condition=SentTo(user_id=10)) is False
            # A holding condition deletes the row it names.
            assert await service.delete(MESSAGE_IDS[0], condition=SentTo(user_id=10)) is True

    async def test_exists_agrees_with_an_in_memory_oracle(self, messages) -> None:
        """The SQL criterion and the fixture's ground truth agree, NULLs included."""
        oracle = {MESSAGE_IDS[0], MESSAGE_IDS[1]}
        for user_id, expected in ((10, {MESSAGE_IDS[0]}), (11, oracle), (99, set())):
            resource = MessageResource(Message, session_factory=messages)
            async with Manifest([resource]):
                found = set(await _search(resource, SentTo(user_id=user_id)))
            assert found == expected


class TestUnconvertibleRaises:
    async def test_a_resource_without_the_rule_raises(self, plain_resource) -> None:
        with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
            await _search(plain_resource, SentTo(user_id=10))

    async def test_a_resource_without_the_rule_refuses_a_write(self, plain_resource) -> None:
        async with await plain_resource.get_service() as service:
            with pytest.raises(UnsupportedFilterError, match="No SQL conversion"):
                await service.delete(MESSAGE_IDS[0], condition=SentTo(user_id=10))

    async def test_a_rule_that_cannot_build_a_criterion_raises(self) -> None:
        class BadRule(SqlFilterRule):
            def handles(self, node: SearchFilter[Any]) -> bool:
                return isinstance(node, SentTo)

            async def convert(self, node, converter, *, column=None, negated=False):
                raise UnsupportedFilterError("cannot resolve this node")

        converter = SqlFilterConverter(
            SqlFilterContext(columns={}, table=Message.__table__),
            (BadRule(), *DEFAULT_RULES),
        )
        with pytest.raises(UnsupportedFilterError, match="cannot resolve"):
            await converter.condition(SentTo(user_id=1))


class TestRuleExtensionPoints:
    async def test_an_app_rule_wins_by_being_first(self, message_resource) -> None:
        """The first rule whose ``handles`` matches wins; order is the override."""
        rules = message_resource.get_filter_rules()
        assert isinstance(rules[0], SentToRule)

    async def test_a_custom_value_leaf_extends_the_value_rule(self) -> None:
        """``ValueRule.operators`` is the extension point for an app's own leaf."""

        class PrefixFilter(SearchFilter[Any]):
            value: str

            def matches(self, value: Any) -> bool:  # pragma: no cover - pushdown only
                raise NotImplementedError

        from resourcey.sql.filter_converter import (
            STANDARD_VALUE_OPERATORS,
            ValueOperator,
            ValueRule,
        )

        def positive(column, value):
            return column.like(f"{value}%")

        def negated(column, value):
            return ~column.like(f"{value}%")

        rule = ValueRule(
            {**STANDARD_VALUE_OPERATORS, PrefixFilter: ValueOperator(positive, negated)}
        )
        converter = SqlFilterConverter(
            SqlFilterContext(columns={"text": Message.__table__.c.text}),
            (*DEFAULT_RULES[:-1], rule),
        )
        node = attr("text", PrefixFilter(value="al"))
        compiled = str(await converter.condition(node))
        assert "LIKE" in compiled.upper()

    async def test_rules_are_frozen_dataclasses(self) -> None:
        assert is_dataclass(SentToRule)
        rule = ValueRule()
        with pytest.raises(FrozenInstanceError):
            rule.operators = {}  # type: ignore[misc]

    async def test_meta_is_shared_across_rules_of_one_conversion(self) -> None:
        """A rule can resolve once and hand the result to a sibling through ``meta``."""
        seen: list[dict[Any, Any]] = []

        class ResolvingRule(SqlFilterRule):
            def handles(self, node: SearchFilter[Any]) -> bool:
                return isinstance(node, SentTo)

            async def convert(self, node, converter, *, column=None, negated=False):
                seen.append(converter.ctx.meta)
                converter.ctx.meta["resolved"] = node.user_id  # type: ignore[attr-defined]
                return true() if not negated else false()

        ctx = SqlFilterContext(columns={})
        converter = SqlFilterConverter(ctx, (ResolvingRule(), *DEFAULT_RULES))
        await converter.condition(SentTo(user_id=7))
        assert seen == [ctx.meta]
        assert ctx.meta["resolved"] == 7

    async def test_apply_skips_a_no_restriction_filter(self) -> None:
        from sqlalchemy import select as sql_select

        converter = SqlFilterConverter(SqlFilterContext(columns={"text": Message.__table__.c.text}))
        stmt = sql_select(Message.__table__.c.id)
        assert await converter.apply(stmt, AllFilter()) is stmt
        applied = await converter.apply(stmt, attr("text", EqFilter(value="a")))
        assert applied is not stmt
