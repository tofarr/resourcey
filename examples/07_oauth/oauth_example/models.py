"""SQLAlchemy models for the OAuth example.

The framework's SQL workflow is **model-first**, so the ORM models are the schema
of record and the framework infers the DTO (and REST models) from them. This
example combines two model sets in one migration:

* the board — ``Thread`` and ``Message`` — a small message board, as in examples
  01 / 04;
* the **local** user store, ``User`` — the internal principal an external
  identity resolves to.

The OAuth tables themselves are the framework's own model-first declarations
(``oauth_clients`` / ``external_identities`` / ``oauth_tokens``), re-exported
here so one Alembic diff (and one ``create_all``) covers the whole schema. Their
tables live in three separate metadata bases
(:class:`~resourcey.auth.auth_oauth_client.OAuthBase` /
:class:`~resourcey.auth.auth_oauth_client.ExternalIdentityBase` /
:class:`~resourcey.auth.auth_oauth_token.OAuthTokenBase`); folding them into this
example's ``Base.metadata`` lets the migration see them.

``Message.author_id`` is the owner column the ``Owner`` policy scopes on; it is
server-stamped from the authenticated principal, never client-supplied, so it
carries a :class:`~resourcey.core.dto.DtoField` in the column's ``info``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_oauth_client import ExternalIdentityBase, OAuthBase
from resourcey.auth.auth_oauth_token import OAuthTokenBase
from resourcey.core.dto import DtoField

OWNER_FIELD = DtoField(in_create_request=False, in_update_request=False)

# The local user id is the internal principal an external ``(issuer, subject)``
# resolves to, and the value the seed script / migration mints (so the
# ``ExternalIdentity`` map has something to point at). Like example 04's
# ``User.id``, it stays client-supplied on create and is immutable.
USER_ID_FIELD = DtoField(in_create_request=True, in_update_request=False)


def _utc_now() -> datetime:
    """Default / onupdate factory for the timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the example's own tables.

    Alembic autogenerates against ``Base.metadata`` (the framework has no
    ``ResourceyBase`` — the ORM models are the schema of record).
    """


class Thread(Base):
    """A shared thread — the board's reference data."""

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
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True, info={"dto_field": OWNER_FIELD}
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


class User(Base):
    """The local principal store: the internal user an external identity maps to.

    The OAuth inbound path resolves ``(issuer, subject)`` through the
    ``ExternalIdentity`` table to a ``user_id`` here, then validates that row is
    live and ``enabled`` — so the local user store is authoritative over the
    identity provider: disable a user and every credential that resolves to it is
    rejected, however valid the external token. There is deliberately no password
    column: authentication stays with the IdP.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, info={"dto_field": USER_ID_FIELD})
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )


# Fold the framework's OAuth tables into this example's metadata so one Alembic
# diff (and one ``create_all``) covers the whole schema.
for _metadata in (
    OAuthBase.metadata,
    ExternalIdentityBase.metadata,
    OAuthTokenBase.metadata,
):
    for _table in _metadata.tables.values():
        if _table.key not in Base.metadata.tables:
            _table.to_metadata(Base.metadata)
