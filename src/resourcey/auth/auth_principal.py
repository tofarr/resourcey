"""The authenticated principal and the authentication seam (issue #131).

Authentication answers *who is the caller*; authorization (the ``Policy`` /
``AuthorizedService`` half of :mod:`resourcey.auth`) answers *what may they do*.
This module is the authentication half and the one code path every method
shares, so a new method plugs in without the policy tier changing.

Vocabulary — three layers that must not be conflated:

* **claims** — what the *credential says* (``sub``, ``exp``, ``email``, …).
  Provenance, never the direct basis of an authorization decision.
* **scopes** — what *this credential* may do. A ceiling the issuer can narrow
  (delegation), intersected with — never a substitute for — the policy.
* **roles** — who/what the *principal* is part of, sourced into a policy by a
  :class:`~resourcey.auth.auth_policy.PolicyResolver`.

:class:`Principal.roles` is the **simple-roles representation**: populated only
when roles are few and credential-carried. The store-backed (RBAC) rung leaves
it empty and looks roles up per request from the principal's ``id``.

:class:`Authenticator` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`,
so a method is selected by ``kind``, and its
:meth:`~Authenticator.authenticate` returns an :class:`AuthResult` — which keeps
*absent* and *invalid* distinguishable. One result therefore yields both a
**lenient** and a **strict** dependency, via :func:`optional_principal` /
:func:`required_principal`.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import inspect
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from resourcey.core.service import NotFoundError
from resourcey.util.models import DiscriminatedUnionMixin

# The ``WWW-Authenticate`` challenge sent with a generic 401. A method may send
# a more specific one (the API-key authenticator names its realm).
AUTH_CHALLENGE = "Bearer"

# The request-state key under which per-request authentication results are
# cached, keyed by ``id(authenticator)``, so several dependencies in one request
# (the builder's and an app route's) do not authenticate twice.
_AUTH_STATE_KEY = "resourcey_auth"

# The call-scoped ``ctx`` key under which the dependency builder stores the
# authenticated principal, so a resource service (which receives the same ctx)
# can read it — e.g. an ``Owner``-scoped resource stamping the owner on a create
# row. Kept a plain string so ``core`` need not import ``auth``.
PRINCIPAL_CTX_KEY = "resourcey_principal"


class PrincipalKind(StrEnum):
    """The kind of authenticated caller.

    ``USER`` is a human principal; ``SERVICE`` is a machine principal;
    ``ANONYMOUS`` is the absence of a principal (:attr:`Principal.id` is
    ``None``).
    """

    USER = "user"
    SERVICE = "service"
    ANONYMOUS = "anonymous"


class Principal(BaseModel):
    """The authenticated caller; ``id is None`` means anonymous.

    ``id`` is the ``user_id`` a policy reduction scopes against. ``external_id``
    is the identity provider's opaque subject (``auth0|abc``, a numeric Google
    id, a DN), carried *alongside* the internal id rather than coerced into it.
    ``roles`` is the simple-roles representation, deliberately empty in the
    store-backed rung; ``scopes`` is a ceiling on the credential; ``claims`` is
    provenance only.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID | None = None
    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    external_id: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    claims: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def anonymous(cls) -> Principal:
        """The anonymous principal (``id is None``)."""
        return cls()

    @classmethod
    def user(cls, id: uuid.UUID, **kwargs: Any) -> Principal:  # noqa: A002
        """A user principal identified by ``id``."""
        return cls(id=id, kind=PrincipalKind.USER, **kwargs)

    @classmethod
    def service(cls, id: uuid.UUID | None = None, **kwargs: Any) -> Principal:  # noqa: A002
        """A machine principal (optionally identified)."""
        return cls(id=id, kind=PrincipalKind.SERVICE, **kwargs)


class AuthResult(BaseModel):
    """One authentication attempt's outcome.

    A credential can be **absent** (no key / no cookie — anonymous) or
    **invalid** (presented but not accepted), so both are carried explicitly
    rather than collapsed into a ``None`` principal.
    """

    model_config = ConfigDict(frozen=True)

    principal: Principal | None = None
    credential_present: bool = False
    credential_valid: bool = False
    refresh_recommended: bool = False
    """Whether the credential should be refreshed (a cookie past its threshold)."""

    @classmethod
    def absent(cls) -> AuthResult:
        """No credential was presented (anonymous)."""
        return cls()

    @classmethod
    def invalid(cls) -> AuthResult:
        """A credential was presented but not accepted."""
        return cls(credential_present=True, credential_valid=False)

    @classmethod
    def authenticated(cls, principal: Principal) -> AuthResult:
        """A credential authenticated as ``principal``."""
        return cls(principal=principal, credential_present=True, credential_valid=True)


class Authenticator(DiscriminatedUnionMixin, ABC):
    """Resolve a request's credential into an :class:`AuthResult`.

    :meth:`dependency` returns the FastAPI dependency the transport uses, which
    caches the result on the request so several consumers authenticate once.
    """

    @abstractmethod
    async def authenticate(self, request: Request) -> AuthResult:
        """Resolve ``request``'s credential into an :class:`AuthResult`."""
        raise NotImplementedError

    def dependency(self) -> Callable[..., Awaitable[AuthResult]]:
        """A FastAPI dependency returning (and caching) this authenticator's result.

        The default declares no OpenAPI security scheme; an authenticator with a
        concrete scheme overrides this to declare it.
        """

        async def dependency(request: Request) -> AuthResult:
            return await resolve_auth_result(request, self)

        return dependency

    def challenge(self) -> str:
        """The ``WWW-Authenticate`` value sent with an authentication ``401``.

        The same challenge is sent for absent and invalid credentials, so a probe
        cannot tell them apart.
        """
        return AUTH_CHALLENGE


class CompositeAuthenticator(Authenticator):
    """An ordered chain of authenticators, tried until one authenticates.

    A deployment offering several methods (e.g. API key *or* cookie) composes
    them into one authenticator. The first method that authenticates wins; if
    none does but at least one saw a credential, the result is *invalid*; if none
    saw a credential, the result is *absent*.
    """

    authenticators: list[Authenticator] = Field(default_factory=list)

    async def authenticate(self, request: Request) -> AuthResult:
        results = [
            await authenticator.authenticate(request) for authenticator in self.authenticators
        ]
        for result in results:
            if result.principal is not None:
                return result
        if any(result.credential_present for result in results):
            return AuthResult.invalid()
        return AuthResult.absent()

    def dependency(self) -> Callable[..., Awaitable[AuthResult]]:
        """A dependency composing the children's dependencies (so their schemes stay visible).

        The signature is synthesised — one ``Depends(child.dependency())``
        parameter per child — so each child's OpenAPI security scheme is declared.
        """
        parameters = [
            inspect.Parameter(
                f"result_{index}",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=Depends(child.dependency()),
                annotation=AuthResult,
            )
            for index, child in enumerate(self.authenticators)
        ]

        async def dependency(**results: AuthResult) -> AuthResult:
            return _combine(results.values())

        dependency.__signature__ = inspect.Signature(parameters=parameters)  # type: ignore[attr-defined]
        dependency.__name__ = "composite_authenticator_dependency"
        return dependency


def _combine(results: Iterable[AuthResult]) -> AuthResult:
    """Reduce child results to one: first authenticated wins, else invalid / absent."""
    ordered = list(results)
    for result in ordered:
        if result.principal is not None:
            return result
    if any(result.credential_present for result in ordered):
        return AuthResult.invalid()
    return AuthResult.absent()


async def resolve_auth_result(request: Request, authenticator: Authenticator) -> AuthResult:
    """Authenticate ``request`` once per authenticator, caching on the request.

    A request may reach the same authenticator through more than one dependency.
    """
    cache: dict[int, AuthResult] | None = getattr(request.state, _AUTH_STATE_KEY, None)
    if cache is None:
        cache = {}
        setattr(request.state, _AUTH_STATE_KEY, cache)
    key = id(authenticator)
    if key not in cache:
        cache[key] = await authenticator.authenticate(request)
    return cache[key]


def store_auth_result(request: Request, result: AuthResult) -> None:
    """Record ``result`` for ``request`` so a downstream dependency reuses it.

    The builder stores the result before it yields the service, so an app
    route's :func:`current_principal` reads the same result.
    """
    setattr(request.state, _AUTH_RESULT_KEY, result)


def _stored_auth_result(request: Request) -> AuthResult | None:
    stored: AuthResult | None = getattr(request.state, _AUTH_RESULT_KEY, None)
    return stored


# A distinct state key for the builder's stored result (not the per-authenticator
# cache), so an app reads exactly what the builder authenticated.
_AUTH_RESULT_KEY = "resourcey_auth_result"


def _unauthorized(challenge: str = AUTH_CHALLENGE) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": challenge},
    )


def optional_principal(
    authenticator: Authenticator,
) -> Callable[..., Awaitable[Principal | None]]:
    """A lenient dependency: anonymous on an absent credential, 401 on an invalid one."""
    authenticate = authenticator.dependency()
    challenge = authenticator.challenge()

    async def dependency(
        request: Request,
        result: AuthResult = Depends(authenticate),  # noqa: B008
    ) -> Principal | None:
        if result.credential_present and not result.credential_valid:
            raise _unauthorized(challenge)
        store_auth_result(request, result)
        return result.principal

    return dependency


def required_principal(authenticator: Authenticator) -> Callable[..., Awaitable[Principal]]:
    """A strict dependency: 401 unless the request is authenticated.

    Derives from the same :class:`AuthResult` as :func:`optional_principal`, so
    both tiers stay one code path.
    """
    authenticate = authenticator.dependency()
    challenge = authenticator.challenge()

    async def dependency(request: Request, result: AuthResult = Depends(authenticate)) -> Principal:  # noqa: B008
        if result.principal is None:
            raise _unauthorized(challenge)
        store_auth_result(request, result)
        return result.principal

    return dependency


async def current_principal(request: Request) -> Principal | None:
    """The principal the dependency builder authenticated, if any.

    ``None`` when the request was not authenticated by a builder (or was
    anonymous).
    """
    stored = _stored_auth_result(request)
    return stored.principal if stored is not None else None


async def principal_is_active(user_resource: Any, principal: Principal) -> bool:
    """Whether ``principal`` is a live user in the configured principal store.

    With no ``user_resource`` every principal is accepted (the credential-only
    posture), and a principal with no id has nothing to look up. With a store,
    the id must be found and pass its ``enabled`` flag, so a disabled user is
    rejected however their credential was issued. A row without an ``enabled``
    attribute is treated as enabled; a *missing* row is a rejection. The lookup
    runs over a **fresh ctx**, so it never adopts the request's storage.
    """
    if user_resource is None or principal.id is None:
        return True
    service = await user_resource.get_service({})
    async with service:
        try:
            user = await service.read(principal.id)
        except NotFoundError:
            return False
    return bool(getattr(user, "enabled", True))
