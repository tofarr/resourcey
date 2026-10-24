"""The OAuth client resources and the ``ExternalIdentity`` mapping (issue #151).

Modeled on the API-key dual-source pattern: one declaration of full storage
truth, two backends (a config ``ListResource`` and a DB ``SqlResource``), and a
narrowing :class:`~resourcey.view.resource_view.ResourceView`.

:class:`OAuthAuthenticator` takes the **inner** resource and never branches on
which rung is in play, mirroring ``ApiKeyAuthenticator.key_resource``.

The *verification* fields (``issuer``, ``jwks_uri``, ``audience``,
``algorithms``) are what ``authenticate`` reads per request; the *flow* fields
(``auth_url``, ``token_url``, ``refresh_url``, ``client_secret``,
``redirect_uri``, ``scopes``) are what the *app* reads.

The DB-backed client resource is a **privilege surface** — writing a row's
``issuer`` / ``jwks_uri`` lets an attacker point token validation at a JWKS they
control — so access must be restricted to administrators.

``ExternalIdentity`` links a provider ``(issuer, subject)`` pair to our internal
``user_id``, so a principal can be correlated **across providers**.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import JSON, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_oauth_config import IdpConfig
from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.list.list_resource import ListResource
from resourcey.sql.sql_resource import SqlResource
from resourcey.view.resource_view import ResourceView

# ``JSON`` with a PostgreSQL ``JSONB`` variant, so a list column compares and
# de-duplicates on both dialects.
JSON_LIST = JSON().with_variant(JSONB(), "postgresql")

# The name of the client-secret field, shared with the service that hides it.
CLIENT_SECRET_FIELD = "client_secret"

# The client secret is never read or searched. Unlike an API key there is no
# one-time reveal — the flow path reads it server-side.
CLIENT_QUERY_SURFACE_HIDDEN: dict[str, dict[str, Any]] = {
    CLIENT_SECRET_FIELD: {"in_read_response": False, "in_search_response": False},
}

# Every secret-bearing field is projected away; the service is the only writer.
TOKEN_SECRET_FIELDS: tuple[str, ...] = ("access_token", "refresh_token", "lease_owner")
TOKEN_QUERY_SURFACE_HIDDEN: dict[str, dict[str, Any]] = {
    field: {"in_read_response": False, "in_search_response": False} for field in TOKEN_SECRET_FIELDS
}


def utc_now() -> datetime:
    """Default factory for the OAuth timestamp columns."""
    return datetime.now(UTC)


class OAuthBase(DeclarativeBase):
    """The declarative base owning the ``oauth_clients`` table."""


class ExternalIdentityBase(DeclarativeBase):
    """The declarative base owning the ``external_identities`` table."""


class OAuthClient(OAuthBase):
    """A stored OAuth / OIDC client row (the model-first counterpart of ``OAuthClientConfig``).

    ``issuer`` is **unique**: it is the sole selector ``find_by_issuer`` uses to
    pick which row's JWKS a presented token is verified against.
    """

    __tablename__ = "oauth_clients"
    __table_args__ = (UniqueConstraint("issuer", name="uq_oauth_client_issuer"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    jwks_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    audience: Mapped[str | None] = mapped_column(String(512), nullable=True)
    algorithms: Mapped[list[str]] = mapped_column(
        JSON_LIST, nullable=False, default=lambda: ["RS256"]
    )
    client_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_secret: Mapped[SecretStr | None] = mapped_column(String(1024), nullable=True)
    auth_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    token_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    refresh_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON_LIST, nullable=False, default=list)
    roles: Mapped[list[str]] = mapped_column(JSON_LIST, nullable=False, default=list)
    refresh_rotates_token: Mapped[bool] = mapped_column(nullable=False, default=False)
    refresh_is_single_use: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ExternalIdentity(ExternalIdentityBase):
    """A provider ``(issuer, subject)`` pair mapped to our internal ``user_id``.

    ``subject`` is always a string: a provider's subject is opaque and must
    never be assumed parseable.
    """

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_identity"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class OAuthClientResource(SqlResource[Any, Any]):
    """The DB-backed client resource, with ``find_by_issuer``."""

    def make_service(self, ctx: Any, session_factory: Any) -> Any:
        from resourcey.auth.auth_oauth_service import OAuthClientService

        return OAuthClientService(self, ctx, session_factory)


class ConfigOAuthClientResource(ListResource[Any, Any]):
    """The read-only config client resource, with ``find_by_issuer``."""

    def make_service(self, ctx: Any, items: list[Any]) -> Any:
        from resourcey.auth.auth_oauth_service import ConfigOAuthClientService

        return ConfigOAuthClientService(self, ctx, items)


class ExternalIdentityResource(SqlResource[Any, Any]):
    """The ``ExternalIdentity`` resource, with the mapping lookup + link seam."""

    def make_service(self, ctx: Any, session_factory: Any) -> Any:
        from resourcey.auth.auth_oauth_service import ExternalIdentityService

        return ExternalIdentityService(self, ctx, session_factory)


def stored_oauth_client_resource(
    *,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
    path: str | None = None,
) -> OAuthClientResource:
    """The DB-backed client resource (the ``OAuthClient`` model, full storage truth)."""
    return OAuthClientResource(
        OAuthClient,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        path=path or "oauth-clients",
    )


class ConfigOAuthClient(BaseModel):
    """A served config client: the declaration with the secret carried as a secret."""

    id: str
    provider: str | None = None
    issuer: str
    jwks_uri: str | None = None
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    client_id: str | None = None
    client_secret: SecretStr | None = None
    auth_url: str | None = None
    token_url: str | None = None
    refresh_url: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    refresh_rotates_token: bool = False
    refresh_is_single_use: bool = False


def config_oauth_client_models(config: IdpConfig) -> list[ConfigOAuthClient]:
    """The served config entries, one per configured client."""
    return [
        ConfigOAuthClient(
            id=entry.id,
            provider=entry.provider,
            issuer=entry.issuer,
            jwks_uri=entry.jwks_uri,
            audience=entry.audience,
            algorithms=list(entry.algorithms),
            client_id=entry.client_id,
            client_secret=entry.client_secret,
            auth_url=entry.auth_url,
            token_url=entry.token_url,
            refresh_url=entry.refresh_url,
            redirect_uri=entry.redirect_uri,
            scopes=list(entry.scopes),
            roles=list(entry.roles),
            refresh_rotates_token=entry.refresh_rotates_token,
            refresh_is_single_use=entry.refresh_is_single_use,
        )
        for entry in config.oauth_clients
    ]


def config_oauth_client_resource(
    config: IdpConfig, *, path: str | None = None
) -> ConfigOAuthClientResource:
    """The read-only, config-backed client resource over the configured entries."""
    return ConfigOAuthClientResource(
        config_oauth_client_models(config),
        model=ConfigOAuthClient,
        path=path or "oauth-clients",
    )


def oauth_client_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed client view: ``client_secret`` is absent from every response.

    The resource is a privilege surface — restrict access to administrators.
    """
    return ResourceView(inner, exposed_field_overrides=CLIENT_QUERY_SURFACE_HIDDEN)


def oauth_token_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed token view: read-only, every secret-bearing field projected away.

    Debuggability is the reason the token table is a resource at all; not
    exposing it is the production default.
    """
    return ResourceView(
        inner,
        exposed_field_overrides=TOKEN_QUERY_SURFACE_HIDDEN,
        exposed_actions=frozenset({Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}),
    )


def external_identity_resource(
    *,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
    path: str | None = None,
) -> Resource[Any, Any]:
    """The ``ExternalIdentity`` resource (the ``(issuer, subject) -> user_id`` map).

    A plain model-first resource, so the mapping can be seeded / administered
    over REST. It is admin-only in a production deployment.
    """
    return ExternalIdentityResource(
        ExternalIdentity,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        path=path or "external-identities",
    )
