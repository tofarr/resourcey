"""The outbound-credential path — ``CredentialProvider`` (issue #151).

OAuth is used for two distinct jobs:

* **inbound** — accept a credential issued by an external identity provider and
  authenticate a caller to *us* (:class:`~resourcey.auth.auth_oauth.OAuthAuthenticator`);
* **outbound** — call an external service (GitHub, Google APIs, Slack) on a
  user's (or the service's) behalf, where the provider is *not* our IdP and the
  resulting token is an **outbound credential**.

The two paths stay distinct in code — inbound *verifies*, outbound *supplies* —
but share one client-config resource and one token service, so the refresh
lifecycle is implemented once.

:class:`CredentialProvider` is the public surface an app calls before an
outbound request: ``async access_token(principal, client) -> str``. The concrete
:class:`OAuthCredentialProvider` implements it over the token service (returning
a still-valid stored token, refreshing under the lease if stale) and performs the
authorization-code / refresh exchanges at the provider's token endpoint. If the
provider's token is itself a JWT we also accept inbound (the common "OIDC
provider that is also an API" case), one client row serves both.

The HTTP client is injected (a duck-typed ``post`` returning an object with
``json()``), so the exchanges are testable without a network.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, SecretStr

from resourcey.auth.auth_oauth_token import OAuthTokenService
from resourcey.auth.auth_principal import Principal
from resourcey.core.errors import InvalidInputError


class TokenResponse(BaseModel):
    """A provider token-endpoint response, normalised.

    Attributes:
        access_token: The access token (plaintext).
        id_token: The OIDC ID token (a JWT), when the provider returned one —
            requesting the ``openid`` scope is what makes it appear. This, not
            ``access_token``, is what OIDC guarantees carries ``iss`` / ``sub`` /
            ``aud`` / ``exp`` for establishing *who signed in*; an access token's
            format and contents are provider-defined and are frequently opaque.
        refresh_token: The refresh token, if the provider returned one.
        expires_at: When the access token expires (derived from ``expires_in``).
        refresh_expires_at: The provider-side refresh-token life, if given.
        scope: The granted scopes, if returned.
    """

    model_config = ConfigDict(frozen=True)

    access_token: str
    id_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime
    refresh_expires_at: datetime | None = None
    scope: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TokenResponse:
        """Build a normalised response from a provider's JSON token payload."""
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise InvalidInputError("Token endpoint returned no access_token")
        now = datetime.now(UTC)
        expires_in = payload.get("expires_in")
        expires_at = (
            now + timedelta(seconds=int(expires_in))
            if isinstance(expires_in, (int, float, str)) and str(expires_in).isdigit()
            else now + timedelta(hours=1)
        )
        id_token = payload.get("id_token")
        refresh = payload.get("refresh_token")
        refresh_expires_in = payload.get("refresh_expires_in")
        refresh_expires_at = (
            now + timedelta(seconds=int(refresh_expires_in))
            if isinstance(refresh_expires_in, (int, float, str))
            and str(refresh_expires_in).isdigit()
            else None
        )
        scope = payload.get("scope")
        return cls(
            access_token=access,
            id_token=id_token if isinstance(id_token, str) and id_token else None,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
            scope=scope if isinstance(scope, str) else None,
        )


class CredentialProvider(ABC):
    """The outbound-credential seam: a valid access token for ``(principal, client)``.

    An app calls :meth:`access_token` before making an outbound request; the
    implementation manages the refresh lifecycle in one place.
    """

    @abstractmethod
    async def access_token(self, principal: Principal, client: Any) -> str:
        """A currently-valid access token for ``principal`` against ``client``."""
        raise NotImplementedError

    @abstractmethod
    async def refresh(self, client: Any, refresh_token: str | None) -> TokenResponse:
        """Exchange ``refresh_token`` for a fresh token at the provider."""
        raise NotImplementedError

    @abstractmethod
    async def exchange_code(
        self, client: Any, *, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenResponse:
        """Exchange an authorization ``code`` for tokens at the provider."""
        raise NotImplementedError

    @abstractmethod
    def authorize_url(
        self,
        client: Any,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        extra_scopes: Iterable[str] = (),
    ) -> str:
        """Build the provider's authorization URL (the login redirect)."""
        raise NotImplementedError


def authorize_url(
    client: Any,
    *,
    state: str,
    redirect_uri: str,
    code_challenge: str | None = None,
    extra_scopes: Iterable[str] = (),
) -> str:
    """Build a provider's authorization URL from a client row / config entry.

    ``response_type=code`` with the client id, redirect URI, and state; the
    requested scopes are the client's configured scopes plus ``extra_scopes``
    (de-duplicated, order-preserving), space-joined, and PKCE parameters are
    added when a challenge is supplied. The interactive login route requests
    ``openid`` through ``extra_scopes`` so the provider includes an ID token in
    the token response (the BFF callback needs it to resolve identity); the
    outbound-only path requests none. Raises
    :class:`~resourcey.core.errors.InvalidInputError` when the client has no
    ``auth_url``.
    """
    if not getattr(client, "auth_url", None):
        raise InvalidInputError(f"Client {client.id!r} has no auth URL")
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client.client_id or "",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    scopes = list(dict.fromkeys([*client.scopes, *extra_scopes]))
    if scopes:
        params["scope"] = " ".join(scopes)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    separator = "&" if "?" in client.auth_url else "?"
    return f"{client.auth_url}{separator}{urlencode(params)}"


class OAuthCredentialProvider(CredentialProvider):
    """The concrete provider over the token service and a duck-typed HTTP client.

    Args:
        token_service: The service that stores / refreshes the token pair.
        http_post: A callable ``(url, data, headers) -> response`` used for the
            token exchanges. The response object must expose ``json()`` and
            ``raise_for_status()``. Injected so the exchanges are testable
            without a network; production wires an ``httpx``-based poster.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(
        self,
        token_service: OAuthTokenService,
        http_post: Callable[..., Any] | None = None,
    ) -> None:
        self._tokens = token_service
        self._http_post = http_post or _default_http_post

    async def access_token(self, principal: Principal, client: Any) -> str:
        """A valid access token, refreshing under the lease when it is stale."""
        if principal.id is None:
            raise InvalidInputError("An anonymous principal has no outbound credential")
        token = await self._tokens.refresh(principal_id=principal.id, client=client, provider=self)
        return _secret(token.access_token)

    async def refresh(self, client: Any, refresh_token: str | None) -> TokenResponse:
        """POST a ``refresh_token`` grant to the provider's token endpoint."""
        if not refresh_token:
            raise InvalidInputError(f"Client {client.id!r} has no refresh token to exchange")
        url = client.refresh_url or client.token_url
        if not url:
            raise InvalidInputError(f"Client {client.id!r} has no token / refresh URL")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if client.client_id:
            payload["client_id"] = client.client_id
        return await self._post_token(client, url, payload)

    async def exchange_code(
        self, client: Any, *, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenResponse:
        """POST an ``authorization_code`` grant to the provider's token endpoint."""
        if not client.token_url:
            raise InvalidInputError(f"Client {client.id!r} has no token URL")
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if client.client_id:
            payload["client_id"] = client.client_id
        if code_verifier:
            payload["code_verifier"] = code_verifier
        return await self._post_token(client, client.token_url, payload)

    def authorize_url(
        self,
        client: Any,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        extra_scopes: Iterable[str] = (),
    ) -> str:
        """Build the authorization URL from the client's ``auth_url`` and config."""
        return authorize_url(
            client,
            state=state,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            extra_scopes=extra_scopes,
        )

    async def _post_token(self, client: Any, url: str, payload: dict[str, str]) -> TokenResponse:
        """POST ``payload`` (as a form) and normalise the JSON token response."""
        data = dict(payload)
        secret = getattr(client, "client_secret", None)
        headers = {"Accept": "application/json"}
        if secret is not None:
            data["client_secret"] = _secret(secret)
        response = self._http_post(url, data=data, headers=headers)
        if inspect.isawaitable(response):
            response = await response
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return TokenResponse.from_payload(response.json())


def _secret(value: Any) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


async def _default_http_post(url: str, *, data: dict[str, str], headers: dict[str, str]) -> Any:
    """An async ``httpx`` POST used by production (tests inject their own)."""
    import httpx  # imported lazily so the package imports without a client

    async with httpx.AsyncClient() as client:
        return await client.post(url, data=data, headers=headers)
