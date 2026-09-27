"""The authorization dependency builder for ``v2`` (issue #127 / #131).

:class:`AuthorizedDependencyBuilder` is a
:class:`~resourcey.v2.http.dependency_builder.DependencyBuilder` (a
``DiscriminatedUnionMixin``, like :class:`~resourcey.v2.http.dependency_builder.DefaultDependencyBuilder`).
Passing it to :func:`~resourcey.v2.http.app.create_app`'s /
:func:`~resourcey.v2.http.app.add_to_app`'s ``dependency_builder=`` argument
secures every resource at once: each resource's per-request service is wrapped
in an :class:`~resourcey.v2.auth.auth_authorized_service.AuthorizedService`.

It **composes two independent, pluggable seams** rather than hard-coding either:

* ``authenticator`` — an :class:`~resourcey.v2.auth.auth_principal.Authenticator`
  resolving the request's credential to an
  :class:`~resourcey.v2.auth.auth_principal.AuthResult` (which distinguishes
  absent from invalid). The default is a **fail-closed**
  :class:`~resourcey.v2.auth.auth_api_key.ApiKeyAuthenticator` (no key resource
  -> every request denied), so a misconfigured app never opens silently.
* ``policy_resolver`` — a
  :class:`~resourcey.v2.auth.auth_policy.PolicyResolver` turning the
  authenticated principal into the policies for the resource. The default is
  :class:`~resourcey.v2.auth.auth_policy.AllowAllResolver`, preserving #118 /
  #127's "a valid credential grants full access".

The **requirement** is the third knob: with ``posture=Posture.OPTIONAL`` an
absent credential is anonymous (a lenient dependency); with the default
``Posture.REQUIRED`` the request must authenticate (a strict dependency). Singling
out a resource (e.g. anonymous reads of one endpoint) is a one-line override
without re-implementing the wiring: :meth:`with_posture` returns a copy with only
the posture changed.

The builder caches its ``AuthResult`` on the request-scoped ctx and stores it so
:func:`~resourcey.v2.auth.auth_principal.current_principal` reads the same result
downstream. After authentication the resource's own service is opened over the
request ctx and wrapped in an ``AuthorizedService`` (entered), which does not
close an inner handed to it already entered — so the storage-ownership rule
("whoever opens the storage owns its commit and close") is preserved.

This module is part of ``v2/auth``: it imports only ``v2``. ``v2/http`` must not
import it (the app supplies the builder), so no cycle exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any

from fastapi import Depends, Request

from resourcey.v2.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.v2.auth.auth_authorized_service import AuthorizedService
from resourcey.v2.auth.auth_policy import AllowAllResolver, PolicyResolver
from resourcey.v2.auth.auth_principal import (
    PRINCIPAL_CTX_KEY,
    Authenticator,
    optional_principal,
    required_principal,
)
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Service
from resourcey.v2.http.dependency_builder import DependencyBuilder, request_ctx


class Posture(StrEnum):
    """How strictly the builder requires authentication.

    ``REQUIRED`` (the default) rejects an unauthenticated request with ``401``;
    ``OPTIONAL`` lets an absent credential through as anonymous (a *presented
    but invalid* credential is still a ``401``), which is what makes
    "anonymous reads, authenticated writes" expressible.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"


class AuthorizedDependencyBuilder(DependencyBuilder):
    """Authenticate, resolve a policy set, then enforce it on every resource's service.

    Attributes:
        authenticator: The authentication method applied to every request.
            Defaults to a fail-closed :class:`ApiKeyAuthenticator`.
        policy_resolver: How the authenticated principal maps to policies.
            Defaults to :class:`AllowAllResolver`.
        posture: ``REQUIRED`` (default) or ``OPTIONAL`` (see :class:`Posture`).
    """

    authenticator: Authenticator = ApiKeyAuthenticator()
    policy_resolver: PolicyResolver = AllowAllResolver()
    posture: Posture = Posture.REQUIRED

    def with_posture(self, posture: Posture) -> AuthorizedDependencyBuilder:
        """A copy of this builder with only ``posture`` changed.

        The one-line override for a differently-guarded endpoint (e.g. anonymous
        reads), without rebuilding the authenticator / resolver wiring.
        """
        return self.model_copy(update={"posture": posture})

    def get_principal_dependency(self) -> Callable[..., Any] | None:
        """The authentication dependency the transport adds to every route.

        It declares the authenticator's security scheme(s) (so OpenAPI shows
        them) and enforces the posture; the authenticated result is cached per
        request, so the service dependency's own authentication is free. The
        resource is not needed — the authenticator and posture are the same for
        every resource.
        """
        if self.posture is Posture.OPTIONAL:
            return optional_principal(self.authenticator)
        return required_principal(self.authenticator)

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., Any]:
        """Compose authentication, the resource's service, and the policy wrapper.

        The returned dependency authenticates first (so an unauthenticated
        request is rejected before the resource opens any storage), resolves the
        principal's policies for this resource, then builds the resource's
        service over the request-scoped ctx and wraps it in an
        :class:`AuthorizedService`.
        """
        # A required posture rejects unless authenticated; an optional one lets
        # an absent credential through as anonymous. Both derive from the same
        # AuthResult, so the requirement tier is one code path.
        if self.posture is Posture.OPTIONAL:
            authenticate: Callable[..., Any] = optional_principal(self.authenticator)
        else:
            authenticate = required_principal(self.authenticator)

        policy_resolver = self.policy_resolver
        id_field = resource.get_id_field()
        resource_name = type(resource).__name__

        async def dependency(
            request: Request,
            principal: Any = Depends(authenticate),  # noqa: B008
        ) -> AsyncIterator[Service[Any, Any]]:
            policies = await policy_resolver.resolve(resource, principal)
            user_id = principal.id if principal is not None else None
            ctx = request_ctx(request)
            # Publish the principal on the call-scoped ctx so a resource service
            # (which receives the same ctx) can read it — e.g. an Owner-scoped
            # resource stamping the owner on a create row the policy deliberately
            # leaves unscoped.
            ctx[PRINCIPAL_CTX_KEY] = principal
            inner = await resource.get_service(ctx)
            service: Service[Any, Any] = AuthorizedService(
                inner,
                policies=policies,
                id_field=id_field,
                resource_name=resource_name,
                user_id=user_id,
                response_private=_response_is_caller_scoped(policies),
            )
            async with service:
                yield service

        return dependency


def _response_is_caller_scoped(policies: list[Any]) -> bool:
    """Whether a response under ``policies`` may differ per caller.

    A response is marked caller-private when any resolved policy scopes to the
    caller (``Policy.scopes_to_caller``). The flag defaults to ``True`` on
    :class:`~resourcey.v2.auth.auth_policy.Policy`, so an unclassified policy is
    treated as caller-scoped (safe); the principal-independent built-ins
    (``AllowAll`` / ``DenyAll`` / ``ReadOnly``) declare ``False`` and keep the
    shared-cache optimisations. This is what stops a shared cache from replaying
    a principal-narrowed body (e.g. an ``Owner`` policy) to another caller.
    """
    return any(getattr(policy, "scopes_to_caller", True) for policy in policies)
