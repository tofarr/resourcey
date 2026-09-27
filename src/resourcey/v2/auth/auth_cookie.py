"""Cookie / session authentication for ``v2`` (issue #131).

:class:`CookieAuthenticator` is an
:class:`~resourcey.v2.auth.auth_principal.Authenticator` whose credential is a
signed, encrypted cookie carrying a JWE (via
:class:`~resourcey.v2.encryption.encryption_service.EncryptionService`) with at
least ``sub`` and ``exp``. It is the cookie counterpart of the API-key
authenticator and plugs into the same
:class:`~resourcey.v2.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`.

Two distinct expiry notions, deliberately:

* the **JWT ``exp``** (inside the encrypted cookie) is *when it is appropriate to
  go back and check the principal is still valid* — a session-freshness window,
  typically short. Past it the cookie is **stale** (not invalid): the request is
  still authenticated, and :attr:`AuthResult.refresh_recommended` is set so the
  application re-issues the cookie.
* the **cookie's own ``Max-Age`` / ``Expires``** (browser-enforced) is how long
  the browser keeps presenting it — typically longer. Once it lapses the browser
  simply stops sending the cookie and the caller is anonymous.

A cookie that cannot be decrypted, names no ``sub``, or is structurally bad is
*invalid*; a cookie with no ``exp`` has no freshness bound (it never goes stale).

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request
from pydantic import ConfigDict

from resourcey.v2.auth.auth_principal import Authenticator, AuthResult, Principal, PrincipalKind
from resourcey.v2.auth.auth_role import roles_from_credential
from resourcey.v2.encryption.encryption_service import EncryptionService

SUB_CLAIM = "sub"
EXP_CLAIM = "exp"
ROLES_CLAIM = "roles"


class CookieAuthenticator(Authenticator):
    """Authenticate a request from an encrypted session cookie.

    Attributes:
        cookie_name: The cookie the JWE is read from.
        encryption_service: The service that decrypts the cookie's JWE. Defaults
            to the process-wide :func:`~resourcey.v2.encryption.encryption_service.get_encryption_service`
            (built from ``EncryptionKeysConfig``), so the common case needs no
            wiring.
        refresh_after: An *optional* extra margin before ``exp``: the cookie is
            considered stale once it is within this window of ``exp`` (or at
            ``exp`` when unset), so a caller can refresh slightly early.
    """

    cookie_name: str = "session"
    encryption_service: EncryptionService | None = None
    refresh_after: timedelta | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _service(self) -> EncryptionService:
        if self.encryption_service is not None:
            return self.encryption_service
        from resourcey.v2.encryption.encryption_service import get_encryption_service

        return get_encryption_service()

    async def authenticate(self, request: Request) -> AuthResult:
        """Decrypt the cookie's JWE into an :class:`AuthResult`.

        Absent cookie -> *absent*; undecryptable / no ``sub`` / structurally bad
        -> *invalid*; otherwise *authenticated*, with ``refresh_recommended``
        when the cookie is past :attr:`refresh_after` (or already past ``exp``).
        """
        raw = request.cookies.get(self.cookie_name)
        if not raw:
            return AuthResult.absent()
        claims = self._decrypt(raw)
        if claims is None:
            return AuthResult.invalid()
        principal = self._principal_from(claims)
        if principal is None:
            return AuthResult.invalid()
        return AuthResult(
            principal=principal,
            credential_present=True,
            credential_valid=True,
            refresh_recommended=self._is_stale(claims),
        )

    def _decrypt(self, raw: str) -> dict[str, Any] | None:
        try:
            claims = self._service().decrypt_jwe_token(raw)
        except Exception:
            # An undecryptable / tampered cookie is a bad credential, not a 500.
            return None
        return claims if isinstance(claims, dict) else None

    def _principal_from(self, claims: dict[str, Any]) -> Principal | None:
        raw_sub = claims.get(SUB_CLAIM)
        if raw_sub is None:
            return None
        try:
            subject = UUID(str(raw_sub))
        except ValueError:
            # ``sub`` must be the principal's UUID; anything else is a bad cookie.
            return None
        return Principal(
            id=subject,
            kind=PrincipalKind.USER,
            roles=roles_from_credential(claims.get(ROLES_CLAIM)),
            claims=_string_claims(claims),
        )

    def _is_stale(self, claims: dict[str, Any]) -> bool:
        """Whether the cookie is past its freshness window and should be re-issued.

        A cookie with no ``exp`` never goes stale (no freshness bound). Otherwise
        the threshold is ``exp`` (or ``exp - refresh_after`` when a threshold is
        configured), and past it the request must go back and re-check the
        principal.
        """
        exp = claims.get(EXP_CLAIM)
        if exp is None:
            return False
        try:
            expires_at = datetime.fromtimestamp(int(exp), tz=UTC)
        except (TypeError, ValueError, OSError):
            return False
        threshold = (
            expires_at - self.refresh_after if self.refresh_after is not None else expires_at
        )
        return datetime.now(UTC) >= threshold

    def dependency(self) -> Callable[..., Any]:
        async def dependency(request: Request) -> AuthResult:
            return await self.authenticate(request)

        dependency.__name__ = "cookie_authenticator_dependency"
        return dependency


def _string_claims(claims: dict[str, Any]) -> dict[str, str]:
    """The claims as a ``str -> str`` mapping for :attr:`Principal.claims`.

    Non-scalar claims (nested objects / lists) are dropped: ``claims`` is
    provenance kept alongside the principal, not a place to carry arbitrary
    structures. In particular the ``roles`` claim is dropped here — it is
    surfaced on :attr:`Principal.roles`, which is a decision input, not
    provenance.
    """
    return {key: value for key, value in claims.items() if isinstance(value, str)}
