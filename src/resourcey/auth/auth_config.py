"""The config-supplied API-key list (issue #118).

A deployment may declare its accepted API keys entirely through the environment:
each entry carries an ``id``, an optional ``name``, and the key's plaintext
value. The plaintext is read as a :class:`~pydantic.SecretStr` and **hashed on
load**, so it is never retained (and never disclosed through the read surface).

Parsed under the process-wide prefix as ``APP_API_KEYS_0_ID`` / ``_NAME`` /
``_KEY`` / ``_PRINCIPAL_ID``, ``APP_API_KEYS_1_*`` … (or the JSON-array form).
Rotation: add a key alongside the old, deploy, then remove the old once no client
presents it.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from resourcey.config.config_base import BaseConfig


class ApiKeyConfig(BaseModel):
    """One configured API key: an identifier, a label, and the plaintext secret.

    ``id`` is the list resource's identifier (a natural key). ``key`` is read as
    a :class:`~pydantic.SecretStr` so it is redacted in logs, then hashed on load.
    """

    id: str
    name: str | None = None
    key: SecretStr
    principal_id: str | None = None
    """An optional principal id a config key authenticates as.

    Without it a config key resolves to an anonymous-id *service* principal. A
    DB-backed key's own ``user_id`` always wins.
    """

    roles: list[str] = Field(default_factory=list)
    """The roles this key authenticates as (the simple-roles vocabulary, #132).

    Short, app-scoped strings carried on the key's definition, populated onto the
    principal with no extra store lookup. An empty list is fail-closed.
    """


class ApiKeysConfig(BaseConfig):
    """The root config block holding the configured API keys.

    ``api_keys``'s order is the served order; two entries sharing an ``id`` would
    collide, so a duplicate is a misconfiguration rather than a second key.
    """

    api_keys: list[ApiKeyConfig] = Field(default_factory=list)


class SessionCookieConfig(BaseConfig):
    """The env-driven session-cookie settings (issue #131).

    Parsed under the process-wide prefix as ``APP_SESSION_COOKIE_*`` (fields are
    prefixed so they do not collide in the shared flat namespace). The JWT
    ``exp`` is a session-freshness window; the cookie TTL is how long the browser
    keeps the cookie.
    """

    session_cookie_name: str = "session"
    session_cookie_ttl_seconds: int = 60 * 60 * 24 * 7
    session_cookie_refresh_after_seconds: int | None = None
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_domain: str | None = None
    session_cookie_path: str = "/"
