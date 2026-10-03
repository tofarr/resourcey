"""SQLAlchemy models for the webhooks example.

Identical to example 01's message board (``Thread`` / ``Message``, a plain
one-to-many relation) — this example is about **triggers**, not a new domain,
so it reuses the familiar shape rather than inventing one. The framework's SQL
workflow is model-first: the ORM model is the schema of record and the
framework infers the DTO (and the six REST models) from it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    """Default / onupdate factory for the timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the webhooks-example tables.

    Alembic autogenerates against ``Base.metadata`` (there is no ``ResourceyBase``
    in the framework — the ORM models are the schema of record).
    """


class Thread(Base):
    """A message-board thread — the parent side of the one-to-many relation.

    Wrapped directly in ``webhooks_example.app`` with
    ``TriggeredResource(..., on_edit=[...])`` — the core, no-config seam.
    """

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


class Message(Base):
    """A message belonging to a single ``Thread`` via ``thread_id``.

    Left as a plain resource in ``webhooks_example.app`` — its trigger is
    attached by the **config-driven** ``TriggerConfig`` /
    ``TriggeredDependencyBuilder`` rung instead, with no change to this model
    or to the resource declaration.
    """

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
