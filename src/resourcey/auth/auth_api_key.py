"""API-key authentication (issue #118, re-expressed in #131).

:class:`ApiKeyAuthenticator` is an
:class:`~resourcey.auth.auth_principal.Authenticator`, so it plugs into
:class:`~resourcey.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
alongside every other method. It resolves a presented API key to a
:class:`~resourcey.auth.auth_principal.Principal`:

* a **DB-backed** key resolves to its owner (``principal.id == row.user_id``)
  when the row carries one, so a policy can scope rows to the key's owner; a key
  with no owner resolves to a **service** principal;
* a **config-list** key resolves to a service principal (optionally named by
  ``principal_id``).

It holds the **inner** key resource (the DB-backed or config-list resource that
exposes ``find_by_key``) and validates a presented key by hashing it and
searching for that digest. It deliberately does not hold the exposed
``ResourceView``, whose ``ViewService`` forwards only the eight standard actions.

Contract:

* two accepted headers — ``X-API-Key`` and, failing that,
  ``Authorization: Bearer <key>``, both declared with ``Security`` so they appear
  in the OpenAPI schema;
* **fail-closed** — no key resource, or a key resource that matches nothing,
  denies every request;
* a **revoked** (``active=False``) or **expired** row denies the request;
* the key check runs over a **fresh ctx**, so it always opens and closes its own
  storage and never adopts the target resource's session.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from resourcey.auth.auth_api_key_resource import hash_api_key
from resourcey.auth.auth_principal import (
    Authenticator,
    AuthResult,
    Principal,
    PrincipalKind,
    principal_is_active,
)
from resourcey.auth.auth_role import roles_from_credential

API_KEY_HEADER_NAME = "X-API-Key"

# ``Bearer`` is the canonical scheme for the ``Authorization`` fallback; the
# dedicated ``X-API-Key`` header has no registered scheme, so the realm names it.
API_KEY_CHALLENGE = 'Bearer realm="api-key"'

# ``auto_error=False`` so the authenticator decides the response; declaring them
# via ``Security`` keeps the schemes visible in the OpenAPI schema.
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

    ``key_resource`` is the **inner** key resource whose service exposes
    ``find_by_key``; ``None`` fails closed. ``principal_id`` is the fixed id for
    **config-list** keys (a DB row's owner always wins). ``user_resource`` is an
    optional principal store the resolved principal is validated against.
    """

    key_resource: Any = None
    principal_id: UUID | None = None
    user_resource: Any = None

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
        principal = self._principal_for(row)
        if not await self._principal_is_active(principal):
            return AuthResult.invalid()
        return AuthResult.authenticated(principal)

    def dependency(self) -> Callable[..., Any]:
        """A FastAPI dependency declaring the two key schemes and returning the result.

        Declares ``X-API-Key`` / ``Bearer`` via ``Security`` so both appear in the
        OpenAPI schema, then defers to :meth:`authenticate` for the decision.
        """
        from resourcey.auth.auth_principal import resolve_auth_result

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

        For a custom router or endpoint that is not secured by the builder.
        """
        presented = x_api_key
        if presented is None and bearer is not None:
            presented = bearer.credentials
        row = await self.lookup_api_key(presented) if presented else None
        if row is None:
            raise _reject()
        principal = self._principal_for(row)
        # The same store check as ``authenticate``, so a custom router cannot
        # accept a key whose principal is missing or disabled.
        if not await self._principal_is_active(principal):
            raise _reject()
        return principal

    async def lookup_api_key(self, presented: str) -> Any | None:
        """The stored key entry for ``presented``, or ``None`` (fail-closed).

        The value is hashed (SHA-256) and the digest searched for; the plaintext is
        never compared. An **inactive** or **expired** row also yields ``None``.
        The lookup runs over a **fresh, call-scoped ctx**, so the key service never
        adopts the target resource's session.
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

        A DB row's owner wins when present; otherwise the row's own
        ``principal_id`` names a service principal, falling back to the
        authenticator's fixed :attr:`principal_id`. The row's
        credential-carried ``roles`` are populated with no extra lookup.
        """
        owner = getattr(row, "user_id", None)
        roles = roles_from_credential(getattr(row, "roles", None))
        if owner is not None:
            return Principal(id=owner, kind=PrincipalKind.USER, roles=roles)
        # A present-but-unparseable id degrades to ``None`` rather than adopting
        # the shared fixed principal, so a malformed binding cannot grant
        # another principal's scope.
        raw_principal_id = getattr(row, "principal_id", None)
        principal_id = (
            _as_uuid(raw_principal_id) if raw_principal_id is not None else self.principal_id
        )
        return Principal(id=principal_id, kind=PrincipalKind.SERVICE, roles=roles)

    async def _principal_is_active(self, principal: Principal) -> bool:
        """Whether ``principal`` is a live user in the configured principal store.

        Delegates to the shared check so the API-key and OAuth methods enforce the
        identical store rule.
        """
        return await principal_is_active(self.user_resource, principal)


def _reject() -> HTTPException:
    """The 401 raised when a presented key is missing, invalid, or not a live principal."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": API_KEY_CHALLENGE},
    )


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

    A row that does not carry these columns (the config-list entry type) is
    treated as live.
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
