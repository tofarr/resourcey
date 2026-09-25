"""Tests for ``v2`` keyset cursor pagination (issue #78).

Paging with ``next_cursor`` walks the whole result set with no gaps or repeats,
and a tampered cursor is rejected. Ordering is fixed to the identifier field;
there is no sort / filter surface yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.service import ServiceError
from resourcey.v2.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.v2.encryption.encryption_service import EncryptionService
from resourcey.v2.sql import cursor as cursor_module
from resourcey.v2.sql.resource import SqlResource


class PaginationBase(DeclarativeBase):
    pass


class Item(PaginationBase):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(50))


def _dto_type() -> type:
    return SqlResource(Item, session_factory=async_sessionmaker()).get_dto_type()


def _encryption() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(
            encryption_key=EncryptionKeyConfig(id="test", value="test-secret-key-for-cursors")
        )
    )


@pytest_asyncio.fixture
async def resource() -> AsyncIterator[SqlResource[Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    res = SqlResource(Item, session_factory=maker, encryption_service=_encryption())
    async with engine.begin() as conn:
        await conn.run_sync(res.metadata.create_all)
    async with res.get_service() as service:
        for i in range(7):
            await service.create(_dto_type()(label=f"item-{i}"))
    yield res
    await engine.dispose()


async def _walk(resource: SqlResource[Any], limit: int) -> list[Any]:
    """Walk every page via ``next_cursor`` and return the collected ids."""
    seen: list[int] = []
    cursor: str | None = None
    async with resource.get_service() as service:
        while True:
            page = await service.search(limit=limit, cursor=cursor)
            seen.extend(item.id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    return seen


async def test_paging_walks_every_row_with_no_gaps_or_repeats(resource):
    ids = await _walk(resource, limit=2)
    assert ids == [1, 2, 3, 4, 5, 6, 7]
    assert len(ids) == len(set(ids))


async def test_last_page_has_no_next_cursor(resource):
    async with resource.get_service() as service:
        page = await service.search(limit=10)
    assert len(page.items) == 7
    assert page.next_cursor is None


async def test_page_does_not_overrun_when_limit_equals_remaining(resource):
    # limit == remaining count on a page: an off-by-one would wrongly advertise
    # a next page.
    async with resource.get_service() as service:
        first = await service.search(limit=4)
        second = await service.search(limit=3, cursor=first.next_cursor)
    assert (len(first.items), first.next_cursor is not None) == (4, True)
    assert (len(second.items), second.next_cursor) == (3, None)


async def test_next_cursor_is_opaque_and_kid_tagged(resource):
    import base64
    import json

    async with resource.get_service() as service:
        page = await service.search(limit=1)
    assert page.next_cursor is not None
    assert page.next_cursor.count(".") == 4
    header_b64 = page.next_cursor.split(".")[0]
    padded = header_b64 + "=" * (-len(header_b64) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded))
    assert header["kid"] == "test"
    # The payload is not the plaintext id.
    assert "item-" not in page.next_cursor


async def test_tampered_cursor_is_rejected(resource):
    async with resource.get_service() as service:
        page = await service.search(limit=1)
        cursor = page.next_cursor
        assert cursor is not None
        segments = cursor.split(".")
        segments[3] = ("A" if segments[3][0] != "A" else "B") + segments[3][1:]
        with pytest.raises(ValueError):
            await service.search(limit=1, cursor=".".join(segments))


async def test_garbage_cursor_is_rejected(resource):
    async with resource.get_service() as service:
        with pytest.raises(ValueError):
            await service.search(limit=1, cursor="not-a-cursor")


async def test_cursor_pagination_requires_an_encryption_service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    res = SqlResource(Item, session_factory=maker)  # no encryption service
    async with engine.begin() as conn:
        await conn.run_sync(res.metadata.create_all)
    async with res.get_service() as service:
        await service.create(_dto_type()(label="x"))
        page = await service.search(limit=1)
        assert page.next_cursor is None
        # A cursor cannot be encoded, so asking for the next page fails clearly.
        with pytest.raises(ServiceError, match="no EncryptionService"):
            await service.search(limit=1, cursor="anything")
    await engine.dispose()


# ---------------------------------------------------------------------------
# cursor module unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [1, 2**63, "abc", 1.5, True],
)
def test_cursor_round_trips_scalar_values(value):
    service = _encryption()
    cursor = cursor_module.encode_cursor(
        service, sort_field=None, ascending=True, sort_key=value, id_value=value
    )
    sort_field, ascending, sort_key, id_value = cursor_module.decode_cursor(service, cursor)
    assert (sort_field, ascending, sort_key, id_value) == (None, True, value, value)


def test_cursor_round_trips_datetime_and_uuid():
    service = _encryption()
    for value in (datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC), uuid4()):
        cursor = cursor_module.encode_cursor(
            service, sort_field="created_at", ascending=False, sort_key=value, id_value=value
        )
        assert cursor_module.decode_cursor(service, cursor) == ("created_at", False, value, value)


@pytest.mark.parametrize(
    "value",
    [
        date(2020, 1, 2),
        time(3, 4, 5),
        Decimal("12.34"),
        object(),
    ],
)
def test_cursor_round_trips_remaining_scalar_types(value):
    service = _encryption()
    cursor = cursor_module.encode_cursor(
        service, sort_field=None, ascending=True, sort_key=value, id_value=value
    )
    decoded = cursor_module.decode_cursor(service, cursor)
    if isinstance(value, object) and type(value) is object:
        # An unknown type falls back to its string repr.
        assert decoded[2] == str(value)
    else:
        assert decoded[2] == value


def test_deserialize_unknown_tag_falls_back_to_the_repr():
    assert cursor_module._deserialize("unknown", "raw") == "raw"


def test_keyset_predicate_compares_the_id_column():
    from sqlalchemy import Column, Integer, MetaData, Table

    table = Table("t", MetaData(), Column("id", Integer, primary_key=True))
    predicate = cursor_module.keyset_predicate(
        sort_column=table.c.id,
        id_column=table.c.id,
        cursor_key=5,
        cursor_id=5,
        ascending=True,
    )
    assert str(predicate.compile()) == "t.id > :id_1"


def test_keyset_predicate_uses_the_sort_key_with_id_tie_breaker():
    from sqlalchemy import Column, Integer, MetaData, Table

    table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("n", Integer))
    ascending = cursor_module.keyset_predicate(
        sort_column=table.c.n,
        id_column=table.c.id,
        cursor_key=3,
        cursor_id=5,
        ascending=True,
    )
    assert str(ascending.compile()) == "t.n > :n_1 OR t.n = :n_2 AND t.id > :id_1"
    descending = cursor_module.keyset_predicate(
        sort_column=table.c.n,
        id_column=table.c.id,
        cursor_key=3,
        cursor_id=5,
        ascending=False,
    )
    assert str(descending.compile()) == "t.n < :n_1 OR t.n = :n_2 AND t.id < :id_1"


def test_keyset_predicate_collapses_descending_on_the_id_column():
    from sqlalchemy import Column, Integer, MetaData, Table

    table = Table("t", MetaData(), Column("id", Integer, primary_key=True))
    predicate = cursor_module.keyset_predicate(
        sort_column=table.c.id,
        id_column=table.c.id,
        cursor_key=5,
        cursor_id=5,
        ascending=False,
    )
    assert str(predicate.compile()) == "t.id < :id_1"
