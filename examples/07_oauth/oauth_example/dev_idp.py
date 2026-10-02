"""The dev identity provider — a runnable local stand-in for a real IdP.

The example's happy path needs a token the authenticator can verify, and a real
provider is not part of a self-contained demo. This module mints a **dev JWKS**
(a single RSA key) and the matching **dev token**, and the app injects the JWKS
fetcher that serves it, so a token signed with the dev key verifies through the
ordinary :class:`~resourcey.auth.auth_oauth.OAuthAuthenticator` code path (the
only thing faked is the network fetch, which is the external boundary).

A real deployment replaces both: point ``APP_OAUTH_CLIENTS_0_JWKS_URI`` at the
provider and drop the injected fetcher. Nothing in the verification path changes.

The key is generated once at import (a process-lifetime dev key). It is
deliberately **not** a secret — a demo token, not a credential.
"""

from __future__ import annotations

import time
from typing import Any

from joserfc import jwt
from joserfc.jwk import RSAKey

# The dev provider's issuer / audience, matching the committed ``.env`` client.
DEV_ISSUER = "https://dev-idp.example"
DEV_AUDIENCE = "oauth-example"

#: The one dev signing key. ``kid`` matches the JWKS the fetcher serves.
_dev_key = RSAKey.generate_key(2048, {"kid": "dev-key", "use": "sig", "alg": "RS256"})


def dev_jwks() -> dict[str, Any]:
    """The dev JWKS document (public key only)."""
    return {"keys": [_dev_key.as_dict(private=False)]}


def make_dev_token(
    *,
    subject: str,
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    expires_in: int = 3600,
    issuer: str = DEV_ISSUER,
    audience: str = DEV_AUDIENCE,
) -> str:
    """Mint a dev RS256 token the app's authenticator will accept.

    Args:
        subject: The external subject (the ``sub`` claim) to resolve.
        roles: A roles claim, surfaced on ``Principal.roles``.
        scopes: Granted scopes, surfaced on ``Principal.scopes``.
        expires_in: Seconds until ``exp``.
        issuer: The ``iss`` claim (must match a configured client).
        audience: The ``aud`` claim (must match the client's audience).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
    }
    if roles:
        payload["roles"] = roles
    if scopes:
        payload["scope"] = " ".join(scopes)
    return jwt.encode({"alg": "RS256", "kid": "dev-key"}, payload, _dev_key)


async def dev_jwks_get(url: str) -> dict[str, Any]:
    """An injectable JWKS fetcher returning the dev document (no network)."""
    return dev_jwks()
