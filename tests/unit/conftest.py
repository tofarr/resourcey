"""Shared test fixtures and model definitions for search_filter tests.

Provides:
- A SQLAlchemy ``User`` ORM model with email, username, enabled, created_at
- An async SQLite session fixture for executing filter SQL end-to-end
- A ``UserSearchFilter`` (BaseSearchFilter[User]) for declarative field tests
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

from resourcey.util.search_filter import BaseSearchFilter


class Base(MappedAsDataclass, DeclarativeBase):
    """Declarative base for test models."""


class User(Base):
    """A minimal user ORM model for filter tests."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(init=False, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Nullable on purpose: exercises the NULL ``in`` divergence between the
    # in-memory and SQL paths (see test_in_null_*).
    nickname: Mapped[str | None] = mapped_column(String(64), default=None)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(init=False, default_factory=lambda: datetime.now())


class UserSearchFilter(BaseSearchFilter[User]):
    """Optional filter clauses for testing.

    Field names follow the `<attr>__<op>` convention so the base class derives
    both the in-memory `matches` predicate and the SQL `filter_sql` clauses
    automatically. Every field is optional; an unset filter matches everything.
    """

    email__contains: str | None = None
    email__eq: str | None = None
    email__in: list[str] | None = None
    username__contains: str | None = None
    username__eq: str | None = None
    username__in: list[str] | None = None
    nickname__in: list[str | None] | None = None
    enabled__eq: bool | None = None
    enabled__ne: bool | None = None
    created_at__gte: datetime | None = None
    created_at__lt: datetime | None = None
    created_at__gt: datetime | None = None
    created_at__lte: datetime | None = None


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Provide an async SQLite session with tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


def new_user(email: str, username: str | None = None, nickname: str | None = None) -> User:
    """Construct a transient User for in-memory filter tests."""
    return User(email=email, username=username or email.split("@")[0], nickname=nickname)


# Make new_user available as a pytest fixture for convenience.
@pytest.fixture
def make_user() -> Callable[..., User]:
    return new_user


@pytest.fixture(autouse=True)
def _reset_resource_caches():
    """No-op: backend caches are now per-instance (issue #51).

    Resources cache their session factory / Mongo client on each *instance*
    (set in ``__aenter__``, cleared in ``__aexit__``). A fresh manifest starts
    clean, so there is nothing to reset between tests. Kept as an autouse
    fixture so legacy tests that referenced it don't break, and so a future
    class-level cache can be cleared here.
    """
    yield
