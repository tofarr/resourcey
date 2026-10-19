"""The OAuth / OIDC client-config block (issue #151, Part 4 of the auth roadmap).

An OAuth client row is declared once with **full storage truth** — the fields
split into two roles that must not be conflated:

* **verification** (what ``OAuthAuthenticator`` reads per request): ``issuer``,
  ``jwks_uri``, ``audience``, ``algorithms``;
* **flow** (what the *app* reads, not the authenticator): ``auth_url``,
  ``token_url``, ``refresh_url``, ``client_secret``, ``redirect_uri``,
  ``scopes``.

A deployment may declare its clients entirely through the environment
(``APP_OAUTH_CLIENTS_<n>_*``) instead of a table; the same declaration backs the
DB rung, so the authenticator never branches on which one is in play.

``refresh_url`` (the provider's *token* endpoint) is unrelated to
``CookieAuthenticator.refresh_after`` (the framework's own cookie-staleness
margin) — do not conflate them.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from resourcey.config.config_base import BaseConfig

# The default signature algorithms an OAuth client accepts. The allowlist is
# authoritative — the token header's ``alg`` is never trusted.
DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)


class OAuthClientConfig(BaseModel):
    """One configured OAuth / OIDC client.

    ``issuer`` selects the row for a presented token and is validated against
    it. ``jwks_uri`` ``None`` disables inbound verification (a pure outbound /
    flow client), and ``audience`` ``None`` skips the ``aud`` check.
    ``client_secret`` is ``None`` for a public client (PKCE).
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
    (``APP_OAUTH_CLIENTS_0_ID`` / ``_ISSUER`` / ``_JWKS_URI`` / …). The block
    also carries the flow-route paths and the refresh-lease / flow-state /
    JWKS-cache tuning. ``oauth_jwks_allowed_hosts`` bounds the SSRF surface of
    a DB-backed client row.
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
