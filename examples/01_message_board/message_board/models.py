"""SQLAlchemy models for the message board.

The ``v2`` SQL workflow is **model-first**: the ORM model is the schema of
record and the framework infers the DTO (and the six REST models) from it. So
this is where the resource fields are declared — column types map back to Python
annotations, nullability widens the annotation to ``ann | None``, and the
columns' ``default`` / ``onupdate`` become the DTO's create / update defaults.

``Message.thread_id`` is a real foreign-key column to ``threads.id``. ``v2``
projects plain columns only (a FK column is an ordinary scalar field), which is
exactly what this example needs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    """Default / onupdate factory for the timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the message-board tables.

    Alembic autogenerates against ``Base.metadata`` (there is no ``ResourceyBase``
    in ``v2`` — the ORM models are the schema of record).
    """


class Thread(Base):
    """A message-board thread — the parent side of the one-to-many relation."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional with a create default, so an omitted ``description`` is supplied by
    # the application and the field stays a writable, nullable create field.
    description: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


class Message(Base):
    """A message belonging to a single ``Thread`` via ``thread_id``."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("threads.id"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )
