"""Framework-signed capability URLs over ``EncryptionService`` (issue #117).

SQL and local file mediums have no external object store to delegate signing to,
so the framework mints its own capability URL: a JWE token carrying only
``{"k": <opaque key>, "op": "put" | "get"}`` and an ``iat`` / ``exp``, produced
by :meth:`~resourcey.encryption.encryption_service.EncryptionService.create_jwe_token`.
The token is served by the routes in :mod:`resourcey.filestore.file_routes`.

Two facts drive the verifier:

* **``decrypt_jwe_token`` does not enforce ``exp``.** It selects the key from
  the ``kid``, decrypts, and returns the raw claim dict; the expiry check is the
  *caller's* responsibility. :func:`verify_signed_url` therefore rejects a token
  whose ``exp`` has passed -- without it a capability URL would be valid forever.
* **The token is a bearer capability.** Anyone holding the URL can use it until
  it expires, so it is short-lived, bound to exactly one ``(key, op)`` pair, and
  the ``op`` is checked against the route. AES-GCM authenticates the claims, so a
  client cannot forge or edit them -- but it can *replay* them, which the TTL
  bounds.

:class:`SignedFileStore` is the base for a medium that uses this scheme: it owns
the mint / verify helpers over an injected
:class:`~resourcey.encryption.encryption_service.EncryptionService` (defaulting
to the process-wide one), so its concrete subclasses implement only the medium
operations.

This module imports no code outside the framework.
"""

from __future__ import annotations

from abc import ABC
from datetime import datetime, timedelta
from typing import cast
from urllib.parse import quote

from pydantic import PrivateAttr

from resourcey.core.errors import InvalidInputError
from resourcey.encryption.encryption_service import (
    EncryptionService,
    get_encryption_service,
    utc_now,
)
from resourcey.filestore.file_store import GET_OPERATION, PUT_OPERATION, FileStore, PresignedUrl

# The route both framework-signed methods are served from. The ``{key}``
# placeholder is substituted with the (already URL-safe) opaque key.
DEFAULT_SIGNED_URL_PATH = "/_files/{key}"

# The HTTP method each capability operation maps to.
_METHOD_FOR_OPERATION = {PUT_OPERATION: "PUT", GET_OPERATION: "GET"}


def signed_url_path(key: str, *, path_template: str = DEFAULT_SIGNED_URL_PATH) -> str:
    """The route path for ``key`` (quoted so an opaque key stays one segment)."""
    return path_template.replace("{key}", quote(key, safe=""))


def mint_signed_url(
    encryption_service: EncryptionService,
    key: str,
    operation: str,
    *,
    expires_in_seconds: int,
    base_url: str = "",
    path_template: str = DEFAULT_SIGNED_URL_PATH,
) -> PresignedUrl:
    """Mint a framework-signed capability URL for one ``(key, operation)``.

    The TTL bounds replay: the token is valid until ``exp`` only. ``base_url``
    is prepended when set (an absolute URL for a cross-origin client); otherwise
    the URL is relative to the API host serving it.
    """
    _validate_operation(operation)
    token = encryption_service.create_jwe_token(
        {"k": key, "op": operation},
        expires_in=timedelta(seconds=expires_in_seconds),
    )
    url = (
        f"{base_url.rstrip('/')}{signed_url_path(key, path_template=path_template)}"
        f"?token={quote(token)}"
    )
    return PresignedUrl(
        url=url,
        method=_METHOD_FOR_OPERATION[operation],
        expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
    )


def verify_signed_url(
    encryption_service: EncryptionService,
    token: str,
    *,
    expected_operation: str,
    now: datetime | None = None,
) -> str:
    """Verify a capability token and return the opaque key it authorizes.

    Rejects, with :class:`~resourcey.core.errors.InvalidInputError` (mapped to
    ``400``):

    * a malformed / tampered token (decryption fails);
    * a token whose ``exp`` has passed -- the codec does not enforce it;
    * a token presented for the wrong operation (a ``put`` token cannot ``get``);
    * a token missing its ``k`` / ``op`` claims.

    The key comparison against the request path is the route's job (it must
    reject a token for key A being used on the route for key B).
    """
    _validate_operation(expected_operation)
    try:
        claims = encryption_service.decrypt_jwe_token(token)
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidInputError(f"Invalid signed URL: {exc}") from exc

    operation = claims.get("op")
    if operation != expected_operation:
        raise InvalidInputError(f"Signed URL is for {operation!r}, not {expected_operation!r}")
    _reject_expired(claims, now=now)
    key = claims.get("k")
    if not isinstance(key, str) or not key:
        raise InvalidInputError("Signed URL is missing its key claim")
    return key


def _reject_expired(claims: dict[str, object], *, now: datetime | None = None) -> None:
    """Raise when the token's ``exp`` has passed (the codec does not check it)."""
    exp = claims.get("exp")
    if exp is None:
        raise InvalidInputError("Signed URL has no expiry")
    current = now or utc_now()
    if current.timestamp() >= float(cast("float | int", exp)):
        raise InvalidInputError("Signed URL has expired")


def _validate_operation(operation: str) -> None:
    if operation not in _METHOD_FOR_OPERATION:
        raise ValueError(f"Unknown signed-URL operation {operation!r}")


class SignedFileStore(FileStore, ABC):
    """A medium whose capabilities are framework-signed and served by the API.

    ``presign_put`` / ``presign_get`` are concrete here (they only mint a JWE);
    a subclass supplies the medium operations plus the URL shape through
    :attr:`signed_url_base_url` / :attr:`signed_url_path_template`.
    """

    signed_url_base_url: str = ""
    signed_url_path_template: str = DEFAULT_SIGNED_URL_PATH

    _encryption: EncryptionService | None = PrivateAttr(default=None)

    def encryption_service(self) -> EncryptionService:
        """The service that signs and verifies this store's capabilities."""
        if self._encryption is None:
            self._encryption = get_encryption_service()
        return self._encryption

    def presign_put(
        self, key: str, *, content_type: str | None, expires_in_seconds: int
    ) -> PresignedUrl:
        return mint_signed_url(
            self.encryption_service(),
            key,
            PUT_OPERATION,
            expires_in_seconds=expires_in_seconds,
            base_url=self.signed_url_base_url,
            path_template=self.signed_url_path_template,
        )

    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        return mint_signed_url(
            self.encryption_service(),
            key,
            GET_OPERATION,
            expires_in_seconds=expires_in_seconds,
            base_url=self.signed_url_base_url,
            path_template=self.signed_url_path_template,
        )

    def verify(self, token: str, *, expected_operation: str) -> str:
        """Verify a capability token; return the key it authorizes."""
        return verify_signed_url(
            self.encryption_service(), token, expected_operation=expected_operation
        )
