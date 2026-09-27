"""SQLAlchemy models for the simple-roles example.

The ``v2`` SQL workflow is **model-first**, so the ORM models here are the schema
of record and the framework infers the DTO (and the six REST models) from them.

Two resources with a deliberate ownership asymmetry (the example's point):

* ``Thread`` — a shared board: its rows have **no owner**. A ``USER`` role reads
  all of them (``threads`` is read-only for that role).
* ``Message`` — a per-author row: ``author_id`` names the owner, and the ``USER``
  role may only read / update / delete **its own** messages (the ``Owner``
  policy). A ``MODERATOR`` reads / updates every message.

There is no auth table: the accepted API keys and their roles live in the
environment (``APP_API_KEYS_*``), so the schema is just ``threads`` and
``messages``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.dto import DtoField

# The ``author_id`` column is server-stamped, never client-supplied: mark it so
# on the column's ``info`` (the documented escape hatch) rather than in the DTO.
# The ``Message`` service fills it from the authenticated principal.
OWNER_FIELD = DtoField(in_create_request=False, in_update_request=False)


def _utc_now() -> datetime:
    """Default / onupdate factory for the timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the simple-roles tables.

    Alembic autogenerates against ``Base.metadata`` (``v2`` has no
    ``ResourceyBase`` — the ORM models are the schema of record).
    """


class Thread(Base):
    """A shared thread — every reader may see the whole board."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


class Message(Base):
    """A message owned by ``author_id`` — the ``Owner`` policy scopes it per row."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("threads.id"), nullable=False, index=True
    )
    # The owner column the ``Owner`` policy scopes on. Nullable so a server-
    # side row (e.g. seeded data) can exist; an ``Owner`` scope simply will not
    # match a row with no author.
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True, info={"dto_field": OWNER_FIELD}
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )
