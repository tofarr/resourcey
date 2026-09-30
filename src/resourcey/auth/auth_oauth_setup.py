"""``configure_oauth`` — the one-stop OAuth wiring helper (issue #151).

This subsystem is more wiring than any prior auth method (two client rungs, a
token table, an identity map, a route registrar), so it ships the same helper
vocabulary the other storage-backed subsystems use. :func:`configure_oauth` is
the "helper for setting it all up": one call builds the client resource from
whichever rung, constructs the token resource + its tables, returns a ready
:class:`~resourcey.auth.auth_oauth.OAuthAuthenticator` (and a
:class:`~resourcey.auth.auth_oauth_provider.CredentialProvider` factory), and
hands back the pieces :func:`~resourcey.auth.auth_oauth_routes.register_oauth_routes`
needs — so an app's entry point reads like the other examples rather than wiring
five objects by hand.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from resourcey.auth.auth_config import SessionCookieConfig
from resourcey.auth.auth_oauth import OAuthAuthenticator
from resourcey.auth.auth_oauth_client import (
    ConfigOAuthClientResource,
    OAuthClientResource,
    config_oauth_client_resource,
    external_identity_resource,
    oauth_client_view,
    oauth_token_view,
    stored_oauth_client_resource,
)
from resourcey.auth.auth_oauth_config import IdpConfig
from resourcey.auth.auth_oauth_provider import CredentialProvider, OAuthCredentialProvider
from resourcey.auth.auth_oauth_routes import register_oauth_routes
from resourcey.auth.auth_oauth_token import (
    OAuthTokenResource,
    create_oauth_token_tables,
    oauth_token_resource,
)
from resourcey.core.resource import Resource
from resourcey.encryption.encryption_service import EncryptionService

# The two client-config rungs.
RUNG_CONFIG = "config"
RUNG_DB = "db"


@dataclass
class OAuthSetup:
    """The assembled OAuth pieces, ready for a manifest / app.

    Attributes:
        config: The OAuth config block.
        client_resource: The **inner** client resource (the authenticator holds
            this; it exposes ``find_by_issuer``).
        client_view: The exposed client view (``client_secret`` projected away);
            register this in the manifest.
        token_resource: The **inner** token resource (the route registrar and the
            outbound provider hold this; it exposes the refresh service).
        token_view: The read-only, secrets-projected token view — register it
            only for debuggability (production hides it).
        identity_resource: The ``ExternalIdentity`` resource.
        authenticator: A ready :class:`OAuthAuthenticator` over the inner client /
            identity resources and the optional principal store.
        session_config: The session-cookie config the flow routes read.
        encryption_service: The service the flow routes mint JWEs with.
    """

    config: IdpConfig
    client_resource: Resource[Any, Any]
    client_view: Resource[Any, Any]
    token_resource: Resource[Any, Any]
    token_view: Resource[Any, Any]
    identity_resource: Resource[Any, Any]
    authenticator: OAuthAuthenticator
    session_config: SessionCookieConfig = field(default_factory=SessionCookieConfig.get_instance)
    encryption_service: EncryptionService | None = None

    def resources(self, *, expose_token: bool = False) -> list[Resource[Any, Any]]:
        """The resources to register: the client view, the identity map, and
        (only when ``expose_token``) the read-only token view.
        """
        resources: list[Resource[Any, Any]] = [
            self.client_view,
            self.identity_resource,
        ]
        if expose_token:
            resources.append(self.token_view)
        return resources

    async def create_tables(self) -> None:
        """Create the ``oauth_clients`` / ``external_identities`` / ``oauth_tokens`` tables.

        The ``create_all`` path; an app driving Alembic instead points it at the
        three metadata bases.
        """
        resource = self.token_resource
        maker = getattr(resource, "_session_factory", None)
        if maker is None:
            raise RuntimeError(
                "create_tables needs a session_factory; construct the setup with one."
            )
        await create_oauth_token_tables(maker)

    def provider(self, token_service: Any, http_post: Any = None) -> CredentialProvider:
        """An outbound :class:`CredentialProvider` over an opened token service."""
        return OAuthCredentialProvider(token_service, http_post)

    def register_routes(self, app: Any, **kwargs: Any) -> Any:
        """Mount the interactive flow routes (see ``register_oauth_routes``)."""
        return register_oauth_routes(
            app,
            self.client_resource,
            token_resource=self.token_resource,
            identity_resource=self.identity_resource,
            config=self.config,
            session_config=self.session_config,
            encryption_service=self.encryption_service,
            **kwargs,
        )


def configure_oauth(
    config: IdpConfig | None = None,
    *,
    rung: str = RUNG_CONFIG,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
    user_resource: Resource[Any, Any] | None = None,
    identity_resource: Resource[Any, Any] | None = None,
    session_config: SessionCookieConfig | None = None,
    encryption_service: EncryptionService | None = None,
    http_get: Any = None,
    path: str | None = None,
) -> OAuthSetup:
    """Build the whole OAuth subsystem in one call.

    Args:
        config: The OAuth config block (default :meth:`IdpConfig.get_instance`).
        rung: ``"config"`` (the read-only env list) or ``"db"`` (the
            ``oauth_clients`` table).
        session_factory / session_manager / name: The token / DB-client session
            source (mirrors ``SqlResource``).
        user_resource: An optional principal store the resolved internal user is
            validated against (live + ``enabled``).
        identity_resource: An explicit identity map (default: build one).
        session_config: The session-cookie config the flow routes read.
        encryption_service: The JWE service (default: the process-wide one).
        http_get: An injectable async ``(url) -> dict`` JWKS fetcher for the
            authenticator (default: the ``httpx`` client; tests inject a fake).
        path: The REST path for the client resource (default ``oauth-clients``).
    """
    resolved_config = config if config is not None else IdpConfig.get_instance()
    session = session_config if session_config is not None else SessionCookieConfig.get_instance()

    client_resource: ConfigOAuthClientResource | OAuthClientResource
    if rung == RUNG_DB:
        client_resource = stored_oauth_client_resource(
            session_factory=session_factory,
            session_manager=session_manager,
            name=name,
            path=path,
        )
    elif rung == RUNG_CONFIG:
        client_resource = config_oauth_client_resource(resolved_config, path=path)
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"Unknown OAuth client rung {rung!r}; use 'config' or 'db'.")

    token_resource: OAuthTokenResource = oauth_token_resource(
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        encryption_service=encryption_service,
    )
    identity: Resource[Any, Any] = (
        identity_resource
        if identity_resource is not None
        else external_identity_resource(
            session_factory=session_factory, session_manager=session_manager, name=name
        )
    )
    authenticator = OAuthAuthenticator(
        client_resource=client_resource,
        identity_resource=identity,
        user_resource=user_resource,
        http_get=http_get,
    )
    return OAuthSetup(
        config=resolved_config,
        client_resource=client_resource,
        client_view=oauth_client_view(client_resource),
        token_resource=token_resource,
        token_view=oauth_token_view(token_resource),
        identity_resource=identity,
        authenticator=authenticator,
        session_config=session,
        encryption_service=encryption_service,
    )
