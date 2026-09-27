"""The config-supplied API-key list (issue #118).

A deployment may declare its accepted API keys entirely through the environment
instead of a table: each entry carries an ``id``, an optional ``name``, and the
key's plaintext value. The plaintext is read as a :class:`~pydantic.SecretStr`
and **hashed on load** — the served entry holds only the SHA-256 digest, so the
plaintext is never retained (and never disclosed through the read surface).

Parsed under the process-wide prefix as ``APP_API_KEYS_0_ID`` / ``_NAME`` /
``_KEY`` / ``_PRINCIPAL_ID``, ``APP_API_KEYS_1_*`` … (or the JSON-array form) by
the shared :func:`~resourcey.v2.util.env_parser.from_env`. Rotation is the v1
behaviour: add a key alongside the old, deploy, then remove the old once no
client presents it.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from resourcey.v2.config.config_base import BaseConfig


class ApiKeyConfig(BaseModel):
    """One configured API key: an identifier, a label, and the plaintext secret.

    Attributes:
        id: The entry's identifier (a natural key — it must be unique and is the
            list resource's identifier).
        name: An optional human-readable label.
        key: The key's plaintext, read as a :class:`~pydantic.SecretStr` so it is
            redacted in logs / ``repr``. It is hashed on load and not retained.
    """

    id: str
    name: str | None = None
    key: SecretStr
    principal_id: str | None = None
    """An optional principal id a config key authenticates as.

    A config list has no owner column, so without this a config key resolves to an
    anonymous-id *service* principal; setting it lets such a key act as a fixed
    principal (e.g. a user id) for a principal-scoped policy. A DB-backed key's
    own ``user_id`` always wins.
    """


class ApiKeysConfig(BaseConfig):
    """The root config block holding the configured API keys.

    ``api_keys``'s order is the served order; the identifier is ``id``, so two
    entries sharing an ``id`` would collide (the list resource's identifier is
    read from the first, so a duplicate is a misconfiguration rather than a
    second key).
    """

    api_keys: list[ApiKeyConfig] = Field(default_factory=list)


class SessionCookieConfig(BaseConfig):
    """The env-driven session-cookie settings (issue #131).

    Parsed under the process-wide prefix as ``APP_SESSION_COOKIE_*`` (field names
    are prefixed so they do not collide with another config block's ``name`` /
    ``path`` in the shared flat namespace). The **JWT ``exp``** is a
    session-freshness window (when to re-check the principal); the **cookie TTL**
    is how long the browser keeps the cookie. Both live here so the application
    that issues and clears the cookie has one place to read them from.

    Attributes:
        session_cookie_name: The cookie name.
        session_cookie_ttl_seconds: Browser cookie lifetime (``Max-Age``).
        session_cookie_refresh_after_seconds: How long before ``exp`` the cookie
            is considered stale and should be re-issued; ``None`` disables
            staleness detection.
        session_cookie_secure: Whether the cookie is HTTPS-only.
        session_cookie_samesite: The ``SameSite`` attribute.
        session_cookie_domain: An optional cookie domain.
        session_cookie_path: The cookie path.
    """

    session_cookie_name: str = "session"
    session_cookie_ttl_seconds: int = 60 * 60 * 24 * 7
    session_cookie_refresh_after_seconds: int | None = None
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_domain: str | None = None
    session_cookie_path: str = "/"
