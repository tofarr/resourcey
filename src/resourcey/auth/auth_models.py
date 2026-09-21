"""ORM models for the auth feature (issue #4).

Ported from ohev2's ``auth_models.py`` + ``user_models.py``, adapted to
resourcey's :class:`~resourcey.resource.sql.ResourceyBase`.

Tables:

* ``users`` — the local user. Has ``email``, ``username``, ``enabled``,
  ``password`` (bcrypt hash, nullable for IdP-only users), and ``idp_user_id``
  (stable IdP subject for callback lookup). Designed to be extended by apps
  that want application-specific user fields (see the ``UserBase`` pattern in
  the resources skill).
* ``idp_refresh_tokens`` — encrypted IdP refresh token + expiry.
* ``idp_access_tokens`` — encrypted IdP access token + expiry, referencing the
  refresh row (1:1, cascade delete).
* ``oauth_clients`` — clients registered to use this project as an OAuth
  provider, with encrypted ``client_secret`` and permitted redirect URIs.
* ``oauth_client_redirect_uris`` — allow-list of redirect URIs (wildcard
  segments supported).
* ``api_keys`` — revocable backing rows for API-key credentials.

All timestamps are timezone-aware (TIMESTAMPTZ).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.util import utc_now


class AuthBase(DeclarativeBase):
    """Declarative base for hand-written auth ORM models.

    Separate from :class:`~resourcey.resource.sql.ResourceyBase` (the base for
    *generated* resource models) so that auth tables do not pollute the
    generated-models metadata — which would cause table-name collisions with
    test resources that happen to use the same table name (e.g. ``users``).

    Both bases share the same async SQLAlchemy 2 ORM conventions; apps that
    want a single metadata registry for migrations can call
    ``ResourceyBase.metadata.update(AuthBase.metadata)`` after importing both.
    """


_TZ = DateTime(timezone=True)


def _aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive).

    SQLite does not preserve timezone info even with ``DateTime(timezone=True)``;
    timestamps read back from SQLite are naive. This helper normalizes them so
    comparisons against ``datetime.now(UTC)`` never mix naive and aware values.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _gen_uuid() -> uuid.UUID:
    return uuid.uuid4()


class TokenType(enum.StrEnum):
    """The kind of credential a token represents.

    COOKIE and ACCESS_TOKEN are short-lived JWE tokens whose ``exp`` is synced
    to the backing IdP access-token row. API_KEY is a long-lived, user-managed
    service credential backed by an ``api_keys`` row. IDP_REFRESH_TOKEN is an
    exchange-only credential backed by an ``idp_refresh_tokens`` row; it is
    never accepted as a bearer token and is rotated only by the auth refresh
    endpoint.
    """

    COOKIE = "cookie"
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"
    IDP_REFRESH_TOKEN = "idp_refresh_token"


class AuthToken(BaseModel):
    """The decrypted view of a credential, normalized across all flows.

    ``enabled`` is resolved by :class:`~resourcey.auth.auth_tokens.TokenService`
    from the user row (and the token's DB row for API_KEY).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    enabled: bool
    expires_at: datetime
    token_type: TokenType
    scopes: frozenset[str] = frozenset()


class User(AuthBase):
    """A local user.

    Designed to be extended: apps that need application-specific user fields
    subclass this model and add columns. The auth layer depends only on the
    fields declared here (``id``, ``email``, ``username``, ``enabled``,
    ``password``, ``idp_user_id``, timestamps).
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
    )
    password: Mapped[str | None] = mapped_column(
        String(2048),
        default=None,
        nullable=True,
    )
    idp_user_id: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )


class IdpRefreshToken(AuthBase):
    """An encrypted IdP refresh token persisted for a local user."""

    __tablename__ = "idp_refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    refresh_token: Mapped[str] = mapped_column(String(8192))
    expires_at: Mapped[datetime] = mapped_column(_TZ)
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )


class IdpAccessToken(AuthBase):
    """An encrypted IdP access token persisted for a local user.

    References its backing :class:`IdpRefreshToken` (1:1, cascade delete).
    The local JWE access token handed to clients carries this row's id and
    has its ``exp`` synced to ``expires_at``.
    """

    __tablename__ = "idp_access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    refresh_token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("idp_refresh_tokens.id", ondelete="CASCADE"),
        index=True,
    )
    access_token: Mapped[str] = mapped_column(String(8192))
    expires_at: Mapped[datetime] = mapped_column(_TZ)
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )


class OAuthClient(AuthBase):
    """A client registered to use this project as an OAuth provider.

    The ``client_secret`` is encrypted at rest via the encryption service.
    """

    __tablename__ = "oauth_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    client_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    client_secret: Mapped[str] = mapped_column(String(8192))
    name: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )


class OAuthClientRedirectUri(AuthBase):
    """A permitted redirect URI for an OAuth client (wildcard segments allowed)."""

    __tablename__ = "oauth_client_redirect_uris"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        index=True,
    )
    uri: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )


class ApiKey(AuthBase):
    """A revocable backing row for an API-key credential.

    The raw key value is ``oh_<base52(128 random bits)>``; it is returned to
    the caller exactly once at create time and never stored. The row persists
    only a SHA-256 ``key_hash`` and a non-secret ``prefix`` for display.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(32))
    creator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str | None] = mapped_column(default=None, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expires_at: Mapped[datetime | None] = mapped_column(
        _TZ,
        default=None,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )


class UserPermission(AuthBase):
    """A per-user permission policy for a resource type (issue #4).

    One row per (user, resource_type, policy). The ``permission`` column stores
    a serialized :class:`~resourcey.auth.permission.Permission` discriminated-union
    object as JSON. At request time the
    :class:`~resourcey.auth.permission_resolver.PermissionResolver` fetches every
    row matching ``(user_id, resource_type)``, deserializes each policy, reduces
    it to a :class:`~resourcey.util.search_filter.SearchFilter` for the requested
    action, and OR-combines them (union model — no deny-wins override).

    Groups/roles are deferred (issue #4 decision #1): permissions are direct
    user-to-policy grants. The ``groups`` argument passed to
    :meth:`~resourcey.auth.permission.Permission.to_search_filter` is always
    empty until group storage is added; :class:`GroupPermission` is shipped now
    for forward-compatibility.

    The ``resource_type`` is the resource's type string (class name or
    configured name), matching the key used by
    :class:`~resourcey.auth.secured_service.SecuredService`.
    """

    __tablename__ = "user_permissions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=_gen_uuid,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(128), index=True)
    permission: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZ,
        default=utc_now,
        server_default=func.clock_timestamp(),
        onupdate=func.now(),
    )
