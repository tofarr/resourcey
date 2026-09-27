"""API-key authentication for ``v2`` (issue #118, re-expressed in #131).

:class:`ApiKeyAuthenticator` is an
:class:`~resourcey.v2.auth.auth_principal.Authenticator` (a
``DiscriminatedUnionMixin``), so it plugs into
:class:`~resourcey.v2.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
alongside every other method. It resolves a presented API key to a
:class:`~resourcey.v2.auth.auth_principal.Principal`:

* a **DB-backed** key resolves to its owner (``principal.id == row.user_id``)
  when the row carries one, so a policy can scope rows to the key's owner; a
  key with no owner resolves to a **service** principal;
* a **config-list** key resolves to a service principal (optionally named by
  ``principal_id``).

It holds the **inner** key resource (the DB-backed or config-list resource that
exposes ``find_by_key``) and validates a presented key by hashing it and
searching for that digest. It deliberately does not hold the exposed
:class:`~resourcey.v2.view.resource_view.ResourceView` (whose ``ViewService``
forwards only the eight standard actions and not ``find_by_key``).

Contract:

* two accepted headers — ``X-API-Key`` and, failing that,
  ``Authorization: Bearer <key>``, both declared with ``Security`` so they appear
  in the OpenAPI schema;
* **fail-closed** — no key resource, or a key resource that matches nothing (an
  empty key set), denies every request;
* a **revoked** (``active=False``) or **expired** (``expires_at`` in the past)
  DB row denies the request;
* the key check runs over a **fresh ctx**, so it always opens and closes its own
  storage and never adopts (or is adopted by) the target resource's session.

The old ``ApiKeyDependencyBuilder`` is gone; wire an ``AuthorizedDependencyBuilder
(authenticator=ApiKeyAuthenticator(key_resource=...))`` instead (or pass the
authenticator to ``create_app`` via such a builder). For a bare FastAPI router,
use :meth:`ApiKeyAuthenticator.dependency` / :meth:`api_key_dependency`.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from resourcey.v2.auth.auth_api_key_resource import hash_api_key
from resourcey.v2.auth.auth_principal import (
    Authenticator,
    AuthResult,
    Principal,
    PrincipalKind,
)
from resourcey.v2.auth.auth_role import roles_from_credential

API_KEY_HEADER_NAME = "X-API-Key"

# The challenge sent with a 401. ``Bearer`` is the canonical scheme for the
# ``Authorization`` fallback; the dedicated ``X-API-Key`` header has no
# registered scheme, so it is described in the realm instead.
API_KEY_CHALLENGE = 'Bearer realm="api-key"'

# Both schemes are declared ``auto_error=False`` so the authenticator, not the
# scheme, decides the response (a single 401 for absent and mismatched alike).
# Declaring them via ``Security`` keeps the schemes visible in the OpenAPI schema.
_api_key_header = APIKeyHeader(
    name=API_KEY_HEADER_NAME,
    scheme_name="ApiKeyHeader",
    auto_error=False,
    description="API key sent as the `X-API-Key` header.",
)

_bearer_scheme = HTTPBearer(
    scheme_name="ApiKeyBearer",
    auto_error=False,
    description="API key sent as `Authorization: Bearer <key>`.",
)


class ApiKeyAuthenticator(Authenticator):
    """Authenticate a request against a stored or configured set of API keys.

    Attributes:
        key_resource: The **inner** key resource (DB-backed or config-list) whose
            service exposes ``find_by_key``. ``None`` (the default) fails closed —
            every request is denied — so a missing configuration never silently
            opens the API.
        principal_id: An optional fixed principal id for **config-list** keys
            (which have no owner). A DB-backed key row's owner (``user_id``)
            always wins when present.
    """

    key_resource: Any = None
    principal_id: UUID | None = None

    async def authenticate(self, request: Any) -> AuthResult:
        """Resolve the request's API key (from either header) to an :class:`AuthResult`."""
        presented = request.headers.get(API_KEY_HEADER_NAME)
        if not presented:
            authorization = request.headers.get("Authorization")
            if authorization and authorization.lower().startswith("bearer "):
                presented = authorization[len("bearer ") :].strip()
        if not presented:
            return AuthResult.absent()
        row = await self.lookup_api_key(presented)
        if row is None:
            return AuthResult.invalid()
        return AuthResult.authenticated(self._principal_for(row))

    def dependency(self) -> Callable[..., Any]:
        """A FastAPI dependency declaring the two key schemes and returning the result.

        Overrides the base to declare ``X-API-Key`` / ``Bearer`` via ``Security``
        so both appear in the OpenAPI schema, then defers to
        :meth:`authenticate` for the decision (a single result, cached by
        :func:`~resourcey.v2.auth.auth_principal.resolve_auth_result`).
        """
        from resourcey.v2.auth.auth_principal import resolve_auth_result

        authenticator = self

        async def dependency(
            request: Request,
            x_api_key: Annotated[str | None, Security(_api_key_header)] = None,
            bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
        ) -> AuthResult:
            return await resolve_auth_result(request, authenticator)

        dependency.__name__ = "api_key_authenticator_dependency"
        return dependency

    def challenge(self) -> str:
        """The API-key realm, so a 401 names the scheme (``Bearer realm="api-key"``)."""
        return API_KEY_CHALLENGE

    async def api_key_dependency(
        self,
        x_api_key: Annotated[str | None, Security(_api_key_header)] = None,
        bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
    ) -> Principal:
        """A standalone FastAPI dependency: 401 unless a valid key is presented.

        Useful on a custom router or endpoint that is not secured by the builder:
        ``APIRouter(dependencies=[Depends(ApiKeyAuthenticator(...).api_key_dependency)])``.
        """
        presented = x_api_key
        if presented is None and bearer is not None:
            presented = bearer.credentials
        row = await self.lookup_api_key(presented) if presented else None
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
                headers={"WWW-Authenticate": API_KEY_CHALLENGE},
            )
        return self._principal_for(row)

    async def lookup_api_key(self, presented: str) -> Any | None:
        """The stored key entry for ``presented``, or ``None`` (fail-closed).

        The value is hashed (SHA-256) and the digest searched for; the plaintext
        is never compared, so no stored credential exists to compare against. No
        key resource yields ``None``. A DB row that is **inactive** or
        **expired** also yields ``None``.

        The lookup runs over a **fresh, call-scoped ctx** (not the request ctx),
        so the key service always opens and closes its own storage and can never
        adopt — or be adopted by — the target resource's session.
        """
        if not presented or self.key_resource is None:
            return None
        service = await self.key_resource.get_service({})
        async with service:
            find = getattr(service, "find_by_key", None)
            if find is None:  # pragma: no cover - the resource is validated upstream
                return None
            row = await find(hash_api_key(presented))
            if row is None:
                return None
            return row if _key_is_live(row) else None

    def _principal_for(self, row: Any) -> Principal:
        """The principal a matched key row resolves to.

        A DB row's owner (``user_id``) wins when present (a user principal); a
        row with no owner, or a config-list entry, resolves to a service
        principal named by the row's own ``principal_id`` (falling back to the
        authenticator's fixed :attr:`principal_id`). The row's
        credential-carried ``roles`` are populated onto the principal with no
        extra lookup, so a role-based policy can read them.
        """
        owner = getattr(row, "user_id", None)
        roles = roles_from_credential(getattr(row, "roles", None))
        if owner is not None:
            return Principal(id=owner, kind=PrincipalKind.USER, roles=roles)
        principal_id = _as_uuid(getattr(row, "principal_id", None)) or self.principal_id
        return Principal(id=principal_id, kind=PrincipalKind.SERVICE, roles=roles)


def _as_uuid(value: Any) -> UUID | None:
    """Coerce a stored principal id (a string in config) to a ``UUID``, or ``None``."""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _key_is_live(row: Any, *, now: datetime | None = None) -> bool:
    """Whether a stored key row is usable: ``active`` and not past ``expires_at``.

    Rows that do not carry these columns (the config-list entry type) are
    treated as live — the config list is the deployment's own declaration, not a
    revocable store.
    """
    active = getattr(row, "active", None)
    if active is not None and not active:
        return False
    expires_at = getattr(row, "expires_at", None)
    if expires_at is not None:
        current = now or datetime.now(UTC)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= current:
            return False
    return True
