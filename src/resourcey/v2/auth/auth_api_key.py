"""API-key authentication for ``v2`` resources (issue #118).

:class:`ApiKeyDependencyBuilder` is a :class:`~resourcey.v2.http.dependency_builder.DependencyBuilder`
(a :class:`~resourcey.v2.util.models.DiscriminatedUnionMixin`, like
:class:`~resourcey.v2.http.dependency_builder.DefaultDependencyBuilder`), so
passing it to :func:`~resourcey.v2.http.app.create_app`'s
``dependency_builder=`` argument secures every resource at once: its
:meth:`get_service_dependency` composes a key check with the resource's own
per-request service dependency. Its :meth:`api_key_dependency` is useful on its
own in any FastAPI router or endpoint.

The builder holds the **inner** key resource — the DB-backed or config-list
resource that exposes ``find_by_key`` — and validates a presented key by hashing
it and searching for that digest. It deliberately does not hold the exposed
:class:`~resourcey.v2.view.resource_view.ResourceView` (whose ``ViewService``
forwards only the eight standard actions and not ``find_by_key``).

Contract:

* two accepted headers — ``X-API-Key`` and, failing that,
  ``Authorization: Bearer <key>``, both declared with ``Security`` so they appear
  in the OpenAPI schema;
* **fail-closed** — no key resource, or a key resource that matches nothing (an
  empty key set), denies every request with ``401``;
* **absent == invalid** — a request with no key and one with a wrong key are
  answered identically (``401`` + ``WWW-Authenticate: Bearer realm="api-key"``),
  so the endpoint does not reveal whether a credential was expected;
* the key check runs **first** and over a **fresh ctx**, so it always opens and
  closes its own storage, before the wrapped resource's service is built.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ConfigDict

from resourcey.v2.auth.auth_api_key_resource import hash_api_key
from resourcey.v2.http.dependency_builder import DefaultDependencyBuilder, DependencyBuilder

API_KEY_HEADER_NAME = "X-API-Key"

# The challenge sent with a 401. ``Bearer`` is the canonical scheme for the
# ``Authorization`` fallback; the dedicated ``X-API-Key`` header has no
# registered scheme, so it is described in the realm instead.
API_KEY_CHALLENGE = 'Bearer realm="api-key"'

# Both schemes are declared ``auto_error=False`` so the dependency, not the
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


class ApiKeyDependencyBuilder(DependencyBuilder):
    """Authenticate every request against a stored or configured set of API keys.

    Attributes:
        key_resource: The **inner** key resource (DB-backed or config-list) whose
            service exposes ``find_by_key``. ``None`` (the default) fails closed —
            every request is denied with ``401`` — so a missing configuration
            never silently opens the API.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key_resource: Any = None

    def get_service_dependency(self, resource: Any) -> Callable[..., Any]:
        """Compose the API-key check with ``resource``'s own service dependency.

        The returned dependency is a drop-in replacement for the resource's
        service dependency: it requires a valid API key, then yields the
        resource's per-request service. The key check is listed first so an
        unauthenticated request is rejected before the resource opens any
        storage.
        """
        service_dependency = DefaultDependencyBuilder().get_service_dependency(resource)
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
        that presents a key matching no stored digest are answered identically
        (``401``, with a ``WWW-Authenticate`` challenge) so the endpoint does not
        reveal whether a credential was expected.
        """
        presented = x_api_key
        if presented is None and bearer is not None:
            presented = bearer.credentials
        if not await self.is_valid_api_key(presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
                headers={"WWW-Authenticate": API_KEY_CHALLENGE},
            )

    async def is_valid_api_key(self, presented: str | None) -> bool:
        """Whether ``presented`` hashes to a stored digest.

        The presented value is hashed (SHA-256) and the digest searched for; the
        plaintext is never compared, so no stored credential exists to compare
        against. Absent / empty input and a mismatched key both yield ``False``,
        and no key resource yields ``False`` (fail-closed).

        The lookup runs over a **fresh, call-scoped ctx** (not the request ctx),
        so the key service always opens and closes its own storage and can never
        adopt — or be adopted by — the target resource's session.
        """
        if not presented or self.key_resource is None:
            return False
        service = await self.key_resource.get_service({})
        async with service:
            find = getattr(service, "find_by_key", None)
            if find is None:  # pragma: no cover - the resource is validated upstream
                return False
            return await find(hash_api_key(presented)) is not None
