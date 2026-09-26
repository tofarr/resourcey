"""The config-supplied API-key list (issue #118).

A deployment may declare its accepted API keys entirely through the environment
instead of a table: each entry carries an ``id``, an optional ``name``, and the
key's plaintext value. The plaintext is read as a :class:`~pydantic.SecretStr`
and **hashed on load** — the served entry holds only the SHA-256 digest, so the
plaintext is never retained (and never disclosed through the read surface).

Parsed under the process-wide prefix as ``APP_API_KEYS_0_ID`` / ``_NAME`` /
``_KEY``, ``APP_API_KEYS_1_*`` … (or the JSON-array form) by the shared
:func:`~resourcey.v2.util.env_parser.from_env`. Rotation is the v1 behaviour:
add a key alongside the old, deploy, then remove the old once no client presents
it.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

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


class ApiKeysConfig(BaseConfig):
    """The root config block holding the configured API keys.

    ``api_keys``'s order is the served order; the identifier is ``id``, so two
    entries sharing an ``id`` would collide (the list resource's identifier is
    read from the first, so a duplicate is a misconfiguration rather than a
    second key).
    """

    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
