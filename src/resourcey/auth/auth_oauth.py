"""``OAuthAuthenticator`` — inbound OAuth / OIDC verification (issue #151).

The inbound half of Part 4: an
:class:`~resourcey.auth.auth_principal.Authenticator` whose credential is a
bearer token issued by an external identity provider. It plugs into the same
:class:`~resourcey.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
and composes with an API-key authenticator through
:class:`~resourcey.auth.auth_principal.CompositeAuthenticator` for "OAuth **or**
API key".

Verification contract:

* Read the presented token (``Authorization: Bearer``), decode its **unverified**
  ``iss`` claim, and select the client row with ``find_by_issuer(iss)``.
* Validate the token against **that row's** JWKS: ``iss``, ``aud``, ``exp`` /
  ``nbf``, and **pin ``alg`` to the row's allowlist** (never the token header's
  ``alg`` — no ``none`` / HMAC-confusion).
* Map claims onto the :class:`~resourcey.auth.auth_principal.Principal`
  vocabulary: ``sub`` -> ``external_id``, ``scope`` / ``scp`` -> ``scopes``, a
  roles claim -> ``roles`` (the simple-roles rung), the remaining claims ->
  ``claims`` (provenance only).
* Resolve ``(iss, sub)`` through the ``ExternalIdentity`` mapping to our internal
  ``user_id``, then reuse the shared principal-store check
  (:func:`~resourcey.auth.auth_principal.principal_is_active`) so a valid
  external credential for a **missing or disabled local user** is rejected — the
  local user store stays authoritative over the IdP. A first-seen ``(iss, sub)``
  with no mapping is **fail-closed** (rejected) by default.

The ``jwks_uri`` from a row is an SSRF vector and the fetch must be **cached**
(``authenticate`` runs per request and must not hit the network each time); an
optional host allowlist bounds it further.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import inspect
import json
import time
from base64 import urlsafe_b64decode
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlparse
from uuid import UUID

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import ConfigDict, PrivateAttr

from resourcey.auth.auth_principal import (
    Authenticator,
    AuthResult,
    Principal,
    PrincipalKind,
    principal_is_active,
    resolve_auth_result,
)
from resourcey.auth.auth_role import roles_from_credential

# The default challenge realm, so a 401 names the scheme.
OAUTH_CHALLENGE = "Bearer"

# The default roles / scopes claim names. A provider varies (Auth0 uses a
# namespaced roles claim; a scoped OAuth token uses ``scope`` or ``scp``).
DEFAULT_ROLES_CLAIM = "roles"
DEFAULT_SCOPE_CLAIMS: tuple[str, ...] = ("scope", "scp")

# The JWT claims always dropped from ``Principal.claims``: ``sub`` is surfaced on
# ``external_id``, the scope / roles claims on ``scopes`` / ``roles`` — all
# decision inputs or the identity itself, not provenance.
_STRUCTURAL_CLAIMS = frozenset({"sub", "iss", "aud", "exp", "nbf", "iat", "jti"})

_bearer_scheme = HTTPBearer(
    scheme_name="OAuthBearer",
    auto_error=False,
    description="OIDC access / ID token sent as `Authorization: Bearer <token>`.",
)


class OAuthAuthenticator(Authenticator):
    """Verify an externally-issued bearer token against its issuer's client row.

    Attributes:
        client_resource: The **inner** client resource (config-list or DB-backed)
            whose service exposes ``find_by_issuer``. ``None`` fails closed.
        identity_resource: The ``ExternalIdentity`` resource whose service exposes
            ``find_by_issuer_subject``. When supplied, ``(iss, sub)`` must map to a
            known ``user_id`` (fail-closed on a first-seen pair). ``None`` keeps a
            credential-only posture (a service principal carrying ``external_id``).
        user_resource: An optional principal store the resolved internal user id
            is validated against (live + ``enabled``), shared with
            :class:`~resourcey.auth.auth_api_key.ApiKeyAuthenticator`.
        jwks_cache_ttl: How long a fetched JWKS is reused. ``None`` re-fetches
            every request (only sensible in tests).
        allowed_hosts: An optional allowlist of ``jwks_uri`` hosts. ``None``
            permits any host.
        roles_claim: The claim naming the principal's roles.
        scope_claims: The claims carrying the credential's granted scopes.
        http_get: An injectable async ``(url) -> dict`` JWKS fetcher. Defaults to
            an ``httpx`` client; tests inject a fake so no network is touched.
        clock: The monotonic clock the JWKS cache reads (injectable for tests).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client_resource: Any = None
    identity_resource: Any = None
    user_resource: Any = None
    jwks_cache_ttl: timedelta | None = timedelta(seconds=300)
    allowed_hosts: list[str] | None = None
    roles_claim: str = DEFAULT_ROLES_CLAIM
    scope_claims: tuple[str, ...] = DEFAULT_SCOPE_CLAIMS
    http_get: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None
    clock: Callable[[], float] = time.monotonic

    _jwks_cache: dict[str, tuple[KeySet, float]] = PrivateAttr(default_factory=dict)

    async def authenticate(self, request: Request) -> AuthResult:
        """Verify the request's bearer token into an :class:`AuthResult`."""
        token = _presented_token(request)
        if not token:
            return AuthResult.absent()
        issuer = _unverified_issuer(token)
        if issuer is None:
            return AuthResult.invalid()
        client = await self._find_client(issuer)
        if client is None:
            return AuthResult.invalid()
        claims = await self._verify(token, client)
        if claims is None:
            return AuthResult.invalid()
        principal = await self._principal(claims, client)
        if principal is None:
            return AuthResult.invalid()
        if not await principal_is_active(self.user_resource, principal):
            return AuthResult.invalid()
        return AuthResult.authenticated(principal)

    def dependency(self) -> Callable[..., Any]:
        """A FastAPI dependency declaring the ``Bearer`` scheme and returning the result."""

        async def dependency(
            request: Request,
            bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
        ) -> AuthResult:
            return await resolve_auth_result(request, self)

        dependency.__name__ = "oauth_authenticator_dependency"
        return dependency

    def challenge(self) -> str:
        """The ``Bearer`` challenge, so a 401 names the scheme."""
        return OAUTH_CHALLENGE

    async def verify_token(self, token: str, client: Any) -> dict[str, Any] | None:
        """Verify an arbitrary token against ``client``'s JWKS; ``None`` on failure.

        The public seam shared with :meth:`authenticate`'s bearer-token path:
        signature, ``iss`` / ``aud`` / ``exp`` / ``nbf``, and ``alg`` pinned to
        the row's allowlist, reusing the same JWKS cache. A caller that must
        verify a token the request-time bearer path never sees — e.g. the login
        callback's OIDC **ID token** — uses this instead of re-implementing (or
        worse, skipping) verification.
        """
        return await self._verify(token, client)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def _find_client(self, issuer: str) -> Any | None:
        """The client row / config entry whose ``issuer`` equals ``issuer``."""
        if self.client_resource is None:
            return None
        service = await self.client_resource.get_service({})
        async with service:
            find = getattr(service, "find_by_issuer", None)
            if find is None:  # pragma: no cover - validated upstream
                return None
            return await find(issuer)

    async def _verify(self, token: str, client: Any) -> dict[str, Any] | None:
        """Verify the token's signature, algorithm, and registered claims.

        Returns the claims on success, ``None`` on any failure. The algorithm
        allowlist comes from the client row, never the token header, so an
        ``alg``-confusion attack is rejected before any signature is trusted.
        """
        jwks_uri = getattr(client, "jwks_uri", None)
        if not jwks_uri:
            return None
        if not self._host_allowed(jwks_uri):
            return None
        keyset = await self._keyset(jwks_uri)
        if keyset is None:
            return None
        algorithms = list(getattr(client, "algorithms", None) or ["RS256"])
        try:
            decoded = jwt.decode(token, keyset, algorithms=algorithms)
        except (JoseError, ValueError, KeyError, TypeError):
            return None
        claims = dict(decoded.claims)
        if not _claims_valid(claims, client):
            return None
        return claims

    async def _keyset(self, jwks_uri: str) -> KeySet | None:
        """The (cached) JWKS for ``jwks_uri``; ``None`` when it cannot be fetched."""
        cached = self._cached(jwks_uri)
        if cached is not None:
            return cached
        try:
            document = self.http_get or _default_jwks_get
            payload: Any = document(jwks_uri)
            if inspect.isawaitable(payload):
                payload = await payload
            keyset = KeySet.import_key_set(cast(Any, dict(payload)))
        except Exception:
            return None
        self._cache(jwks_uri, keyset)
        return keyset

    def _host_allowed(self, url: str) -> bool:
        if self.allowed_hosts is None:
            return True
        host = urlparse(url).hostname
        return host is not None and host in self.allowed_hosts

    # ------------------------------------------------------------------
    # Claims -> principal
    # ------------------------------------------------------------------

    async def _principal(self, claims: dict[str, Any], client: Any) -> Principal | None:
        """Map verified claims onto a principal, resolving the local user id."""
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        issuer = str(claims.get("iss"))
        user_id = await self._resolve_user_id(issuer, subject)
        if user_id is None and self.identity_resource is not None:
            # A configured mapping with no entry is fail-closed: a first-seen
            # external principal is rejected unless the app auto-provisions.
            return None
        roles = roles_from_credential(claims.get(self.roles_claim)) | roles_from_credential(
            getattr(client, "roles", None)
        )
        scopes = _scopes_from(claims, self.scope_claims)
        kind = PrincipalKind.USER if user_id is not None else PrincipalKind.SERVICE
        return Principal(
            id=user_id,
            kind=kind,
            external_id=subject,
            roles=roles,
            scopes=scopes,
            claims=_provenance_claims(claims, self.roles_claim, self.scope_claims),
        )

    async def _resolve_user_id(self, issuer: str, subject: str) -> UUID | None:
        """Map ``(issuer, subject)`` to our internal user id, or ``None``."""
        if self.identity_resource is None:
            return None
        service = await self.identity_resource.get_service({})
        async with service:
            find = getattr(service, "find_by_issuer_subject", None)
            if find is None:  # pragma: no cover - validated upstream
                return None
            mapping = await find(issuer, subject)
        if mapping is None:
            return None
        raw = getattr(mapping, "user_id", None)
        if isinstance(raw, UUID):
            return raw
        try:
            return UUID(str(raw))
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # JWKS cache
    # ------------------------------------------------------------------

    def _cached(self, jwks_uri: str) -> KeySet | None:
        if self.jwks_cache_ttl is None:
            return None
        entry = self._jwks_cache.get(jwks_uri)
        if entry is None:
            return None
        keyset, expiry = entry
        if self.clock() >= expiry:
            del self._jwks_cache[jwks_uri]
            return None
        return keyset

    def _cache(self, jwks_uri: str, keyset: KeySet) -> None:
        if self.jwks_cache_ttl is None:
            return
        self._jwks_cache[jwks_uri] = (
            keyset,
            self.clock() + self.jwks_cache_ttl.total_seconds(),
        )


def _presented_token(request: Request) -> str | None:
    """The bearer token from the ``Authorization`` header, or ``None``."""
    authorization = request.headers.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[len("bearer ") :].strip() or None
    return None


def _unverified_issuer(token: str) -> str | None:
    """Decode the token's ``iss`` claim **without verifying** (for row selection).

    Reading the issuer before verification is safe: the value is used only to
    *select* which keys to verify against, and the signature / ``iss`` are
    re-checked against that row's JWKS afterwards. A malformed token yields
    ``None``.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    issuer = payload.get("iss") if isinstance(payload, dict) else None
    return issuer if isinstance(issuer, str) and issuer else None


def _claims_valid(claims: dict[str, Any], client: Any) -> bool:
    """Validate ``iss`` / ``aud`` / ``exp`` / ``nbf`` against the client row.

    ``decode`` verifies the signature and algorithm but does **not** enforce the
    registered claims, so they are checked here with joserfc's registry. An
    ``aud`` of ``None`` on the row skips the audience check.
    """
    now = int(time.time())
    kwargs: dict[str, Any] = {}
    issuer = getattr(client, "issuer", None)
    if issuer:
        kwargs["iss"] = {"essential": True, "value": issuer}
    audience = getattr(client, "audience", None)
    if audience:
        kwargs["aud"] = {"essential": True, "value": audience}
    try:
        jwt.JWTClaimsRegistry(now=now, **kwargs).validate(claims)
    except (JoseError, ValueError, TypeError):
        return False
    return True


def _scopes_from(claims: dict[str, Any], scope_claims: Iterable[str]) -> frozenset[str]:
    """The granted scopes from a space-delimited string or a list claim."""
    for name in scope_claims:
        raw = claims.get(name)
        if isinstance(raw, str):
            return frozenset(part for part in raw.split() if part)
        if isinstance(raw, (list, tuple)):
            return frozenset(str(part) for part in raw if str(part))
    return frozenset()


def _provenance_claims(
    claims: dict[str, Any], roles_claim: str, scope_claims: Iterable[str]
) -> dict[str, str]:
    """The remaining scalar claims, for provenance only.

    Structural claims (``sub`` / ``iss`` / ``exp`` / …) and the roles / scopes
    claims are dropped — they are surfaced on the principal's dedicated fields,
    which are decision inputs, not provenance.
    """
    excluded = _STRUCTURAL_CLAIMS | {roles_claim} | set(scope_claims)
    return {
        key: value
        for key, value in claims.items()
        if key not in excluded and isinstance(value, str)
    }


async def _default_jwks_get(url: str) -> Mapping[str, Any]:
    """Fetch a JWKS document over HTTP (production default)."""
    import httpx  # imported lazily so the package imports without a client

    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload
