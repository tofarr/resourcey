"""Environment-configured API-key authentication for resources (issue #63).

This module is the first piece of the ``resourcey.auth2`` package, which is
intended to replace ``resourcey.auth`` and therefore must never import it. It
implements the simplest authentication posture from #63: a fixed set of API
keys supplied through configuration grants access, with no users, no sessions,
and no database.

:class:`ApiKeyDependencyBuilder` is a
:class:`~resourcey.config.config_dependency.DependencyBuilder`, so selecting it
on :class:`~resourcey.config.config_framework.FrameworkConfig` (via
``DEPENDENCY_BUILDER_CLASS``) secures every resource at once: its
:meth:`~ApiKeyDependencyBuilder.get_service_dependency` composes an API-key
check with the resource's own per-request service dependency. Its
:meth:`~ApiKeyDependencyBuilder.api_key_dependency` is useful on its own in any
FastAPI router or endpoint.

The accepted keys are read from the environment by the standard config
machinery. A ``DependencyBuilder`` resolved through
:class:`~resourcey.config.lazy_field.LazyField` is built with
``from_env(cls, prefix="DEPENDENCY_BUILDER")``, so the keys are the JSON array
in ``DEPENDENCY_BUILDER_API_KEYS`` or the indexed
``DEPENDENCY_BUILDER_API_KEYS_0``, ``DEPENDENCY_BUILDER_API_KEYS_1``, ... — the
list form is what supports key rotation (add the new key, then remove the old
one once no client presents it).

An empty key list is a deliberate fail-closed posture: every request is denied
with ``403``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, SecretStr

from resourcey.config.config_dependency import DependencyBuilder
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as
from resourcey.resource.errors import ResourceyConfigError

if TYPE_CHECKING:
    from resourcey.resource.base import BaseResource

API_KEY_HEADER_NAME = "X-API-Key"

# Both schemes are declared ``auto_error=False`` so the dependency, not the
# scheme, decides the response (a single 403 for absent and mismatched alike).
# Declaring them via ``Security`` keeps the schemes visible in the OpenAPI
# schema (issue #62: authentication stays a FastAPI ``Depends``).
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


class ApiKeyDependencyBuilder(DependencyBuilder):
    """Authenticate every request against a configured list of API keys.

    A list (rather than a single key) so keys can be rotated: add the new key,
    deploy, then remove the old one once no client presents it. Any configured
    key authenticates the request; the principal is not modelled (this posture
    grants access to every resource).

    Instantiating with no keys is valid and fails closed — every request is
    denied with ``403`` — so a missing configuration never silently opens the
    API.
    """

    api_keys: list[SecretStr] = Field(
        default_factory=list,
        description=(
            "Accepted API keys. Any one of them authenticates a request; an "
            "empty list denies every request (fail-closed)."
        ),
    )

    def get_service_dependency(self, resource: BaseResource) -> Callable[..., Any]:
        """Compose the API-key check with ``resource``'s own service dependency.

        The returned dependency is a drop-in replacement for the resource's
        :meth:`~resourcey.resource.base.BaseResource.get_service_dependency`:
        it requires a valid API key, then yields the resource's per-request
        service. The key check is listed first so an unauthenticated request is
        rejected before the resource opens any storage.
        """
        service_dependency = resource.get_service_dependency
        authenticate = self.api_key_dependency

        async def dependency(
            _authenticated: None = Depends(authenticate),
            service: Any = Depends(service_dependency),  # noqa: B008
        ) -> AsyncIterator[Any]:
            yield service

        return dependency

    async def api_key_dependency(
        self,
        x_api_key: Annotated[str | None, Security(_api_key_header)] = None,
        bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
    ) -> None:
        """FastAPI dependency: accept the request iff it presents a valid key.

        The key is read from the ``X-API-Key`` header or, failing that, from the
        ``Authorization: Bearer`` header. A request that presents no key and one
        that presents a key matching none of :attr:`api_keys` are answered
        identically (``403``) so the endpoint does not reveal whether a
        credential was expected.
        """
        presented = x_api_key
        if presented is None and bearer is not None:
            presented = bearer.credentials
        if not self.is_valid_api_key(presented):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing API key.",
            )

    def is_valid_api_key(self, presented: str | None) -> bool:
        """Whether ``presented`` matches any configured key.

        Each candidate is compared with :func:`secrets.compare_digest` so the
        comparison is constant-time for the presented value; the comparison
        against a shorter configured key therefore does not leak a matching
        prefix.
        """
        if not presented:
            return False
        candidate = presented.encode()
        return any(
            secrets.compare_digest(candidate, key.get_secret_value().encode())
            for key in self.api_keys
        )


def get_api_key_dependency() -> Callable[..., Any]:
    """The active config's :meth:`ApiKeyDependencyBuilder.api_key_dependency`.

    Resolves the builder from
    :class:`~resourcey.config.config_framework.FrameworkConfig` and returns its
    bound dependency so any router can require the configured API key without
    knowing how the posture is wired::

        router = APIRouter(dependencies=[Depends(get_api_key_dependency())])

    Resolve this when building the router (e.g. in an app factory), not at
    module import time — config must be read at runtime (see
    :mod:`resourcey.config.config_runtime`).

    Raises :class:`~resourcey.resource.errors.ResourceyConfigError` when the
    configured builder is not an :class:`ApiKeyDependencyBuilder`, since there
    is then no API key to require.
    """
    builder = get_config_as(FrameworkConfig).dependency_builder
    if not isinstance(builder, ApiKeyDependencyBuilder):
        raise ResourceyConfigError(
            f"The configured DependencyBuilder is {type(builder).__name__}, not "
            "ApiKeyDependencyBuilder; there is no API key to require."
        )
    return builder.api_key_dependency
