"""The OAuth token table and its service — the refresh lifecycle (issue #151).

This is the one **durable** OAuth storage tier. Client / IdP config is the
config-or-DB resource (:mod:`resourcey.auth.auth_oauth_client`); ephemeral flow
state (``state``, PKCE verifier, code) is carried in a short-TTL JWE, not a
table; the tokens themselves live here, **encrypted at rest** through
:class:`~resourcey.encryption.encryption_service.EncryptionService`.

The cookie is never the token: the refresh token lives only, encrypted, in this
table, and a session cookie carries an opaque handle the server maps back to the
row. Encoding a provider credential into a cookie or an API key is explicitly
out.

Refresh serialization (the hard part)
-------------------------------------
Some providers **rotate** the refresh token on every use (the old one is
invalidated) and some allow a refresh token to be used **only once** (a second
concurrent use fails, or invalidates the whole grant). A cluster running several
processes can therefore race: both read the same refresh token, both refresh,
and the loser holds an invalidated token. The refresh must be serialized across
**processes**, not just within one event loop.

The lock is a **lease / compare-and-swap column**, not ``SELECT … FOR UPDATE``:
SQLite (the unit-test database) accepts ``with_for_update()`` and silently drops
the clause, and Mongo has no row lock at all, so the guarantee cannot rest on it.
Each row carries ``lease_owner`` + ``lease_until`` and a refresh is claimed with
a conditional ``UPDATE``; the affected row count tells the caller whether it won.
The winner refreshes and persists the new (and rotated) token **atomically with
releasing the lease**; a loser polls the row briefly and, once ``expires_at`` is
in the future, uses the new access token — no second refresh is issued. The lease
TTL bounds the wait and self-heals a crashed claimer (an expired lease is
reclaimable). ``FOR UPDATE`` may be layered on as a Postgres-only optimization,
never as the contract.

A refresh failure (a rotated-and-invalidated token) marks the row **revoked** and
requires re-authentication, rather than retrying forever.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import DateTime, String, UniqueConstraint, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.errors import InvalidInputError
from resourcey.encryption.encryption_service import EncryptionService, get_encryption_service
from resourcey.sql.sql_resource import SqlResource
from resourcey.sql.sql_service import SqlService

if TYPE_CHECKING:
    from resourcey.auth.auth_oauth_provider import CredentialProvider

# The provider's client row type is duck-typed: any object exposing ``id`` /
# ``token_url`` / ``refresh_url`` / ``client_id`` / ``client_secret`` /
# ``refresh_rotates_token`` / ``refresh_is_single_use`` works, so the token
# service never branches on the config-vs-DB rung.

# How long a caller polls for the winning refresher before giving up, and the
# poll interval. Both are small: the winner's exchange is a single round trip.
_REFRESH_POLL_INTERVAL_SECONDS = 0.05
_REFRESH_POLL_ATTEMPTS = 40


def utc_now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


class OAuthTokenBase(DeclarativeBase):
    """The declarative base owning the ``oauth_tokens`` table."""


class OAuthToken(OAuthTokenBase):
    """A stored OAuth token pair, encrypted at rest.

    ``principal_id`` is **our** internal ``uuid.UUID`` (never the provider
    subject). ``client_id`` is the :class:`OAuthClient` it was issued for. The
    pair is unique, so a principal has at most one token per client.

    ``access_token`` / ``refresh_token`` are :class:`~pydantic.SecretStr` columns
    holding JWE ciphertext (the service encrypts before binding). ``expires_at``
    drives our session ``exp``; ``refresh_expires_at`` is the optional
    provider-side refresh-token life. ``lease_owner`` / ``lease_until`` are the
    refresh lease (compare-and-swap, not a row lock).
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("principal_id", "client_id", name="uq_oauth_token"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[str] = mapped_column(String(128), index=True)
    access_token: Mapped[SecretStr] = mapped_column(String(8192))
    refresh_token: Mapped[SecretStr | None] = mapped_column(String(8192), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scope: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class StoredToken(BaseModel):
    """A token pair with **plaintext** credentials, the shape the service returns.

    The row stores ciphertext; :class:`OAuthTokenService` decrypts at the
    boundary, so a caller (the outbound provider, the session cookie mint) works
    with usable values.
    """

    model_config = ConfigDict(frozen=True)

    principal_id: uuid.UUID
    client_id: str
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime
    refresh_expires_at: datetime | None = None
    scope: str | None = None
    revoked: bool = False


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive datetime (SQLite round-trip) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class OAuthTokenService(SqlService[Any, Any]):
    """Store / get / revoke / refresh for the token table.

    Wraps :class:`~resourcey.encryption.encryption_service.EncryptionService` for
    the encrypt-at-rest boundary and owns the refresh lifecycle (lease / CAS
    serialization, rotation-aware atomic persist). The generic ``read`` /
    ``search`` actions (used by the debug-only exposed resource) return the
    ciphertext columns unchanged — the secret fields are projected away by
    :func:`~resourcey.auth.auth_oauth_client.oauth_token_view` anyway.
    """

    def __init__(
        self,
        resource: SqlResource[Any, Any],
        ctx: MutableMapping[Any, Any],
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encryption_service: EncryptionService | None = None,
        lease_seconds: int = 30,
    ) -> None:
        super().__init__(resource, ctx, session_factory)
        self._oauth_encryption = encryption_service or get_encryption_service()
        self._lease_seconds = lease_seconds
        self._lease_owner = uuid.uuid4().hex

    # ------------------------------------------------------------------
    # store / get / revoke
    # ------------------------------------------------------------------

    async def store(
        self,
        *,
        principal_id: uuid.UUID,
        client_id: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime,
        refresh_expires_at: datetime | None = None,
        scope: str | None = None,
    ) -> StoredToken:
        """Upsert the ``(principal_id, client_id)`` token pair (encrypted at rest).

        The access / refresh tokens are encrypted before binding, so the row
        never holds a usable provider credential. An existing pair is replaced
        (a re-login or a refresh), and the lease is cleared.

        The initial insert (no row yet) is attempted inside a ``SAVEPOINT``: a
        concurrent first-time store for the same ``(principal_id, client_id)``
        (e.g. a double-submitted login) can race the read-then-insert and hit
        the ``uq_oauth_token`` unique constraint. Rather than surface that as an
        unhandled ``IntegrityError``, the savepoint rolls back and the loser
        falls back to updating the row the winner just inserted.
        """
        session = self._active_session()
        table = self._resource.table
        now = utc_now()
        values: dict[str, Any] = {
            "principal_id": principal_id,
            "client_id": client_id,
            "access_token": self._encrypt(access_token),
            "refresh_token": self._encrypt(refresh_token) if refresh_token is not None else None,
            "expires_at": expires_at,
            "refresh_expires_at": refresh_expires_at,
            "scope": scope,
            "lease_owner": None,
            "lease_until": None,
            "updated_at": now,
        }
        existing = await self._row(principal_id, client_id)
        if existing is None:
            insert_values = {**values, "id": uuid.uuid4(), "created_at": now}
            try:
                async with session.begin_nested():
                    await session.execute(table.insert().values(**insert_values))
            except IntegrityError:
                await session.execute(
                    update(table)
                    .where(table.c.principal_id == principal_id, table.c.client_id == client_id)
                    .values(**values)
                )
        else:
            await session.execute(
                update(table).where(table.c.id == existing["id"]).values(**values)
            )
        return StoredToken(
            principal_id=principal_id,
            client_id=client_id,
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token) if refresh_token is not None else None,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
            scope=scope,
        )

    async def get(self, principal_id: uuid.UUID, client_id: str) -> StoredToken | None:
        """The decrypted token pair for ``(principal_id, client_id)``, or ``None``."""
        row = await self._row(principal_id, client_id)
        if row is None:
            return None
        return self._decrypt_row(row)

    async def revoke(self, principal_id: uuid.UUID, client_id: str) -> None:
        """Delete the stored pair (a re-auth is required to obtain a new one)."""
        session = self._active_session()
        table = self._resource.table
        await session.execute(
            delete(table).where(
                table.c.principal_id == principal_id, table.c.client_id == client_id
            )
        )

    async def revoke_all(self, principal_id: uuid.UUID) -> None:
        """Delete every pair for ``principal_id``."""
        session = self._active_session()
        table = self._resource.table
        await session.execute(delete(table).where(table.c.principal_id == principal_id))

    # ------------------------------------------------------------------
    # refresh (lease / CAS serialization)
    # ------------------------------------------------------------------

    async def refresh(
        self,
        *,
        principal_id: uuid.UUID,
        client: Any,
        provider: CredentialProvider,
        skew: timedelta = timedelta(seconds=30),
    ) -> StoredToken:
        """Return a usable access token, refreshing under a lease if stale.

        The correctness contract: at most one refresher runs at a time per
        ``(principal, client)``; the rest observe the result. A still-fresh token
        is returned as-is. Otherwise the row is claimed with a conditional update
        (the lease); the winner performs the exchange and persists the new (and
        rotated) token atomically with releasing the lease, while a loser polls
        briefly for the winner's result. A refresh failure marks the row revoked
        and raises :class:`~resourcey.core.errors.InvalidInputError`.
        """
        current = await self.get(principal_id, client.id)
        if current is None:
            raise InvalidInputError(
                f"No stored token for principal {principal_id} and client {client.id!r}"
            )
        if _as_utc(current.expires_at) > utc_now() + skew:
            return current

        if not await self._claim(principal_id, client.id):
            return await self._await_fresh(principal_id, client.id)

        try:
            refreshed = await provider.refresh(client, _secret(current.refresh_token))
        except Exception as exc:
            # A rotated-and-invalidated refresh token is unusable: revoke and
            # require re-authentication rather than retrying forever.
            await self.revoke(principal_id, client.id)
            raise InvalidInputError(
                f"Refresh failed for principal {principal_id} and client {client.id!r}; "
                "the stored token has been revoked and re-authentication is required."
            ) from exc
        return await self.store(
            principal_id=principal_id,
            client_id=client.id,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
            refresh_expires_at=refreshed.refresh_expires_at,
            scope=refreshed.scope,
        )

    async def _claim(self, principal_id: uuid.UUID, client_id: str) -> bool:
        """Try to win the refresh lease with a conditional update (the CAS).

        The ``WHERE`` clause admits the row only when it is unleased or its lease
        has expired, so a crashed claimer self-heals. The affected row count is
        the whole answer — no ``FOR UPDATE``, so the primitive works on SQLite
        and Mongo alike.
        """
        session = self._active_session()
        table = self._resource.table
        now = utc_now()
        stmt = (
            update(table)
            .where(
                table.c.principal_id == principal_id,
                table.c.client_id == client_id,
                or_(table.c.lease_owner.is_(None), table.c.lease_until < now),
            )
            .values(
                lease_owner=self._lease_owner,
                lease_until=now + timedelta(seconds=self._lease_seconds),
            )
        )
        result = await session.execute(stmt)
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def _await_fresh(self, principal_id: uuid.UUID, client_id: str) -> StoredToken:
        """Poll for the winning refresher's result (no second refresh is issued)."""
        for _ in range(_REFRESH_POLL_ATTEMPTS):
            await asyncio.sleep(_REFRESH_POLL_INTERVAL_SECONDS)
            row = await self._row(principal_id, client_id)
            if row is None:
                raise InvalidInputError("Stored token was revoked during refresh")
            token = self._decrypt_row(row)
            if _as_utc(token.expires_at) > utc_now():
                return token
        raise InvalidInputError(
            "Timed out waiting for a concurrent token refresh; retry the request."
        )

    # ------------------------------------------------------------------
    # storage helpers
    # ------------------------------------------------------------------

    async def _row(self, principal_id: uuid.UUID, client_id: str) -> Any | None:
        session = self._active_session()
        table = self._resource.table
        stmt = select(table).where(
            table.c.principal_id == principal_id, table.c.client_id == client_id
        )
        return (await session.execute(stmt)).mappings().first()

    def _decrypt_row(self, row: Any) -> StoredToken:
        return StoredToken(
            principal_id=row["principal_id"],
            client_id=row["client_id"],
            access_token=SecretStr(self._decrypt(row["access_token"])),
            refresh_token=(
                SecretStr(self._decrypt(row["refresh_token"]))
                if row["refresh_token"] is not None
                else None
            ),
            expires_at=_as_utc(row["expires_at"]),
            refresh_expires_at=(
                _as_utc(row["refresh_expires_at"])
                if row["refresh_expires_at"] is not None
                else None
            ),
            scope=row["scope"],
        )

    def _encrypt(self, value: str) -> str:
        return self._oauth_encryption.encrypt_value(value)

    def _decrypt(self, value: Any) -> str:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        return self._oauth_encryption.decrypt_value(raw)


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


class OAuthTokenResource(SqlResource[Any, Any]):
    """The token resource, served with :class:`OAuthTokenService`."""

    def __init__(
        self, *args: Any, encryption_service: EncryptionService | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._oauth_encryption = encryption_service

    def make_service(
        self, ctx: MutableMapping[Any, Any], session_factory: async_sessionmaker[AsyncSession]
    ) -> Any:
        return OAuthTokenService(
            self, ctx, session_factory, encryption_service=self._oauth_encryption
        )


def oauth_token_resource(
    *,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
    path: str | None = None,
    encryption_service: EncryptionService | None = None,
) -> OAuthTokenResource:
    """The token resource (the ``OAuthToken`` model with the refresh service)."""
    return OAuthTokenResource(
        OAuthToken,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        path=path or "oauth-tokens",
        encryption_service=encryption_service,
    )


async def create_oauth_token_tables(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Create the OAuth tables for an app that uses ``create_all`` rather than Alembic."""
    from resourcey.auth.auth_oauth_client import ExternalIdentityBase, OAuthBase

    async with session_factory() as session:
        connection = await session.connection()
        await connection.run_sync(OAuthBase.metadata.create_all)
        await connection.run_sync(ExternalIdentityBase.metadata.create_all)
        await connection.run_sync(OAuthTokenBase.metadata.create_all)
