"""The authorization dependency builder for ``v2`` (issue #127).

:class:`AuthorizedDependencyBuilder` is a
:class:`~resourcey.v2.http.dependency_builder.DependencyBuilder` (a
``DiscriminatedUnionMixin``, like
:class:`~resourcey.v2.http.dependency_builder.DefaultDependencyBuilder` and
:class:`~resourcey.v2.auth.auth_api_key.ApiKeyDependencyBuilder`). Passing it to
:func:`~resourcey.v2.http.app.create_app`'s ``dependency_builder=`` argument
secures every resource at once: each resource's per-request service is wrapped
in an :class:`~resourcey.v2.auth.auth_authorized_service.AuthorizedService`.

It **composes** authentication rather than re-implementing it. The supplied
``authenticator`` (by default #118's
:class:`~resourcey.v2.auth.auth_api_key.ApiKeyDependencyBuilder`) is reused for
its reusable ``api_key_dependency`` — the key check runs first and over a fresh
ctx, exactly as #118 documents. After it succeeds, the resource's own service is
opened over the request-scoped ctx and wrapped in an ``AuthorizedService``, then
entered. The wrapper does not close an inner handed to it already entered, so the
storage-ownership rule ("whoever opens the storage owns its commit and close")
is preserved.

The policy is a single :class:`~resourcey.v2.auth.auth_policy.Policy` for the
whole app, defaulting to
:class:`~resourcey.v2.auth.auth_policy.AllowAll` — which, together with a valid
API key, is exactly #118's posture ("any valid key grants full access"). A
per-principal policy store (users / groups / roles) is the later rung; this
issue carries one policy and passes ``user_id=None``.

This module is part of ``v2/auth``: it imports only ``v2``. ``v2/http`` must not
import it (the app supplies the builder), so no cycle exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, Request

from resourcey.v2.auth.auth_api_key import ApiKeyDependencyBuilder
from resourcey.v2.auth.auth_authorized_service import AuthorizedService
from resourcey.v2.auth.auth_policy import AllowAll, Policy
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Service
from resourcey.v2.http.dependency_builder import DependencyBuilder, request_ctx


class AuthorizedDependencyBuilder(DependencyBuilder):
    """Authenticate, then enforce a :class:`Policy` on every resource's service.

    Attributes:
        authenticator: The API-key builder whose reusable ``api_key_dependency``
            gates every request. Defaults to a fail-closed
            :class:`ApiKeyDependencyBuilder` (no key resource -> every request
            denied with 401), so a misconfigured app never opens silently.
        policy: The authorization rule enforced on every action. Defaults to
            :class:`AllowAll` (any authenticated caller gets full access), so a
            deployment that only sets the authenticator keeps #118's posture.
    """

    authenticator: ApiKeyDependencyBuilder = ApiKeyDependencyBuilder()
    policy: Policy = AllowAll()

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., Any]:
        """Compose the auth check, the resource's service, and the policy wrapper.

        The returned dependency requires a valid credential, then builds the
        resource's service over the request-scoped ctx and wraps it in an
        :class:`AuthorizedService`. The auth dependency is listed first so an
        unauthenticated request is rejected before the resource opens any
        storage.
        """
        authenticate = self.authenticator.api_key_dependency
        policy = self.policy
        id_field = resource.get_id_field()
        resource_name = type(resource).__name__

        async def dependency(
            request: Request,
            _authenticated: None = Depends(authenticate),
        ) -> AsyncIterator[Service[Any, Any]]:
            inner = await resource.get_service(request_ctx(request))
            service: Service[Any, Any] = AuthorizedService(
                inner,
                policy=policy,
                id_field=id_field,
                resource_name=resource_name,
                user_id=None,
            )
            async with service:
                yield service

        return dependency
