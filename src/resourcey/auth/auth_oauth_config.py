"""The OAuth / OIDC client-config block (issue #151, Part 4 of the auth roadmap).

An OAuth client row is declared once with **full storage truth** — the fields
split into two roles that must not be conflated (see
:class:`~resourcey.auth.auth_oauth_client.OAuthClient`):

* **verification** (what :class:`~resourcey.auth.auth_oauth.OAuthAuthenticator`
  reads per request): ``issuer``, ``jwks_uri``, ``audience``, ``algorithms``;
* **flow** (what the *app* reads, not the authenticator): ``auth_url``,
  ``token_url``, ``refresh_url``, ``client_secret``, ``redirect_uri``,
  ``scopes``.

A deployment may declare its clients entirely through the environment
(``APP_OAUTH_CLIENTS_<n>_*``) instead of a table; the same declaration backs the
DB rung, so the authenticator never branches on which one is in play.

Note ``refresh_url`` (the provider's *token* endpoint, used to exchange a
refresh token) is unrelated to ``CookieAuthenticator.refresh_after`` (the
framework's own cookie-staleness margin) — do not conflate them.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from resourcey.config.config_base import BaseConfig

# The default signature algorithms an OAuth client accepts. RS256 is the
# overwhelming default for an OIDC provider; an app narrows or widens it per
# client. The allowlist is authoritative — the token header's ``alg`` is never
# trusted (no algorithm confusion).
DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)


class OAuthClientConfig(BaseModel):
    """One configured OAuth / OIDC client.

    Attributes:
        id: The client's identifier (a natural key — unique, and the list
            resource's identifier).
        provider: An optional human-readable provider name (e.g. ``"github"``).
        issuer: The expected token issuer (``iss``). Selects the row for a
            presented token and is validated against it.
        jwks_uri: The provider's JWKS endpoint. The token signature keys are
            fetched from here (and cached). ``None`` disables inbound
            verification (a pure outbound / flow client).
        audience: The expected audience (``aud``), normally the ``client_id``.
            ``None`` skips the ``aud`` check.
        algorithms: The signature-algorithm allowlist. The token's ``alg`` must
            be in it; the header's value is never trusted on its own.
        client_id: The OAuth client id (used by the flow / outbound path).
        client_secret: The OAuth client secret, or ``None`` for a public client
            (PKCE). Read as a :class:`~pydantic.SecretStr` and hidden from every
            response.
        auth_url: The provider's authorization endpoint (the login redirect).
        token_url: The provider's token endpoint (the code exchange).
        refresh_url: The provider's token endpoint for a refresh exchange
            (often the same as ``token_url``).
        redirect_uri: Our callback URL registered with the provider.
        scopes: The scopes to request.
        roles: The roles a token from this client authenticates as (the
            simple-roles vocabulary, #132), carried on the credential.
        refresh_rotates_token: Whether the provider rotates the refresh token on
            every use (the old one is invalidated).
        refresh_is_single_use: Whether a refresh token may be used only once (a
            second concurrent use fails or invalidates the whole grant).
    """

    id: str
    provider: str | None = None
    issuer: str
    jwks_uri: str | None = None
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: list(DEFAULT_ALGORITHMS))
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


class IdpConfig(BaseConfig):
    """The OAuth / OIDC config block, parsed under the process-wide prefix.

    ``oauth_clients`` is the config-rung client list
    (``APP_OAUTH_CLIENTS_0_ID`` / ``_ISSUER`` / ``_JWKS_URI`` / … or the
    JSON-array form). The block also carries the flow-route paths and the
    refresh-lease / flow-state / JWKS-cache tuning the OAuth subsystem needs.

    Attributes:
        oauth_clients: The configured clients (the config rung).
        oauth_login_path: The interactive-flow login route path.
        oauth_callback_path: The interactive-flow callback route path.
        oauth_refresh_path: The session-refresh route path.
        oauth_state_cookie_name: The cookie the flow state / PKCE verifier is
            carried in (an HttpOnly, short-TTL JWE — never a table).
        oauth_flow_state_ttl_seconds: How long a login state / verifier stays
            valid (the flow is seconds-lived).
        oauth_refresh_lease_seconds: The refresh-lease TTL that bounds how long
            a losing refresher waits, and how long a crashed claimer's lease
            blocks a retry (self-healing).
        oauth_jwks_cache_ttl_seconds: How long a fetched JWKS is reused, so
            ``authenticate`` does not hit the network per request.
        oauth_jwks_allowed_hosts: An optional host allowlist for ``jwks_uri``.
            ``None`` (the default) permits any host; a list bounds the SSRF
            surface of a DB-backed client row.
    """

    oauth_clients: list[OAuthClientConfig] = Field(default_factory=list)
    oauth_login_path: str = "oauth/login"
    oauth_callback_path: str = "oauth/callback"
    oauth_refresh_path: str = "oauth/refresh"
    oauth_state_cookie_name: str = "oauth_state"
    oauth_flow_state_ttl_seconds: int = 600
    oauth_refresh_lease_seconds: int = 30
    oauth_jwks_cache_ttl_seconds: int = 300
    oauth_jwks_allowed_hosts: list[str] | None = None
