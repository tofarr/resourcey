"""The authorization dependency builder (issue #127 / #131).

:class:`AuthorizedDependencyBuilder` is a
:class:`~resourcey.http.dependency_builder.DependencyBuilder`. Passing it to
``create_app``'s / ``add_to_app``'s ``dependency_builder=`` argument secures
every resource at once: each resource's per-request service is wrapped in an
:class:`~resourcey.auth.auth_authorized_service.AuthorizedService`.

It **composes two independent, pluggable seams**:

* ``authenticator`` — an :class:`~resourcey.auth.auth_principal.Authenticator`
  resolving the request's credential to an ``AuthResult``. The default is a
  **fail-closed** :class:`~resourcey.auth.auth_api_key.ApiKeyAuthenticator`, so a
  misconfigured app never opens silently.
* ``policy_resolver`` — a
  :class:`~resourcey.auth.auth_policy.PolicyResolver` turning the authenticated
  principal into the policies for the resource. The default is
  :class:`~resourcey.auth.auth_policy.AllowAllResolver`.

The **requirement** is the third knob: ``posture=Posture.OPTIONAL`` lets an
absent credential through as anonymous; the default ``Posture.REQUIRED`` rejects
it. :meth:`with_posture` returns a copy with only the posture changed.

This module is part of ``resourcey.auth``; it imports only lower framework
layers. ``http`` must not import it (the app supplies the builder), so no cycle
exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any

from fastapi import Depends, Request

from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_authorized_service import AuthorizedService
from resourcey.auth.auth_policy import AllowAllResolver, PolicyResolver
from resourcey.auth.auth_principal import (
    PRINCIPAL_CTX_KEY,
    Authenticator,
    current_principal,
    optional_principal,
    required_principal,
)
from resourcey.core.resource import Resource
from resourcey.core.service import Service
from resourcey.http.dependency_builder import DependencyBuilder, request_ctx


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
    """Authenticate, resolve a policy set, then enforce it on every resource's service."""

    authenticator: Authenticator = ApiKeyAuthenticator()
    policy_resolver: PolicyResolver = AllowAllResolver()
    posture: Posture = Posture.REQUIRED

    def with_posture(self, posture: Posture) -> AuthorizedDependencyBuilder:
        """A copy of this builder with only ``posture`` changed.

        The one-line override for a differently-guarded endpoint, without
        rebuilding the authenticator / resolver wiring.
        """
        return self.model_copy(update={"posture": posture})

    def get_principal_dependency(self) -> Callable[..., Any] | None:
        """The authentication dependency the transport adds to every route.

        It declares the authenticator's security scheme(s) and enforces the
        posture; the result is cached per request, so the service dependency's
        own authentication is free.
        """
        if self.posture is Posture.OPTIONAL:
            return optional_principal(self.authenticator)
        return required_principal(self.authenticator)

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., Any]:
        """Compose authentication, the resource's service, and the policy wrapper.

        Authenticates first, so an unauthenticated request is rejected before the
        resource opens any storage.
        """
        # Both postures derive from the same AuthResult.
        if self.posture is Posture.OPTIONAL:
            authenticate: Callable[..., Any] = optional_principal(self.authenticator)
        else:
            authenticate = required_principal(self.authenticator)

        async def dependency(
            request: Request,
            principal: Any = Depends(authenticate),  # noqa: B008
        ) -> AsyncIterator[Service[Any, Any]]:
            service = await self.build_service(request, resource)
            async with service:
                yield service

        return dependency

    async def build_service(
        self, request: Request, resource: Resource[Any, Any]
    ) -> Service[Any, Any]:
        """The policy-wrapped service, for a route that selects resources at runtime.

        The principal comes from :func:`current_principal` rather than a
        ``Depends`` parameter: the transport's per-route
        :meth:`get_principal_dependency` has already authenticated and stored it,
        so a runtime-selected resource resolves against the same principal with
        no second authentication.
        """
        principal = await current_principal(request)
        policies = await self.policy_resolver.resolve(resource, principal)
        user_id = principal.id if principal is not None else None
        ctx = request_ctx(request)
        # Publish the principal on the call-scoped ctx so a resource service can
        # read it — e.g. an Owner-scoped resource stamping the owner on create.
        ctx[PRINCIPAL_CTX_KEY] = principal
        inner = await resource.get_service(ctx)
        return AuthorizedService(
            inner,
            policies=policies,
            id_field=resource.get_id_field(),
            resource_name=type(resource).__name__,
            user_id=user_id,
            response_private=_response_is_caller_scoped(policies),
        )


def _response_is_caller_scoped(policies: list[Any]) -> bool:
    """Whether a response under ``policies`` may differ per caller.

    The flag defaults to ``True`` on ``Policy``, so an unclassified policy is
    treated as caller-scoped (safe) and a shared cache cannot replay a
    principal-narrowed body to another caller.
    """
    return any(getattr(policy, "scopes_to_caller", True) for policy in policies)
