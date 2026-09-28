"""SQLAlchemy models for the simple-roles example.

The ``v2`` SQL workflow is **model-first**, so the ORM models here are the schema
of record and the framework infers the DTO (and the six REST models) from them.

Three tables (the example's point):

* ``Thread`` — a shared board: its rows have **no owner**. A ``USER`` role reads
  all of them (``threads`` is read-only for that role).
* ``Message`` — a per-author row: ``author_id`` names the owner, and the ``USER``
  role may only read / update / delete **its own** messages (the ``Owner``
  policy). A ``MODERATOR`` reads / updates every message.
* ``User`` — a stored **principal**. Part 2 carried roles on the credential, so
  a role needed no lookup; but the principal a key acts as was still a bare
  credential field. ``User`` makes it a row, so a principal can be enumerated,
  disabled, and (later) joined to groups / roles. The accepted API keys and their
  roles still live in the environment (``APP_API_KEYS_*``); ``User`` is the
  identity the key's ``PRINCIPAL_ID`` points at.

The ``User`` resource is the **identity** store, not a privilege store: its
surface is narrowed to read-only and the app's role rules make it admin-only, so
one principal cannot enumerate another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.core.dto import DtoField

# The ``author_id`` column is server-stamped, never client-supplied: mark it so
# on the column's ``info`` (the documented escape hatch) rather than in the DTO.
# The ``Message`` service fills it from the authenticated principal.
OWNER_FIELD = DtoField(in_create_request=False, in_update_request=False)

# The ``User.id`` is the principal's identity and is chosen by whoever creates
# the row (the seed script mints fixed UUIDs the API keys point at), so — unlike
# the conventional server-generated ``id`` — it stays client-supplied on create.
# It is still immutable, so it is excluded from updates.
USER_ID_FIELD = DtoField(in_create_request=True, in_update_request=False)


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


class User(Base):
    """A stored principal — the identity an API key's ``PRINCIPAL_ID`` points at.

    This is the identity store the simple-roles example adds: a ``USER`` key's
    ``PRINCIPAL_ID`` must name an ``enabled`` row here, and the row's ``enabled``
    flag is authoritative (disabling it revokes every key that acts as it). There
    is deliberately no password / credential column — authentication stays with
    the API key — and no roles column, because roles are still credential-carried
    (Part 2). Stored groups / roles are a later rung.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, info={"dto_field": USER_ID_FIELD}
    )
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # A client-side default (not a server default) so the application supplies it;
    # it drops out of the create request and an update may still toggle it.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )
