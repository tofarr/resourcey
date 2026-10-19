"""Framework-signed download capability URLs over ``EncryptionService`` (issue #117, #158).

SQL and local mediums have no external object store to delegate signing to, so
the framework mints its own **download** capability: a JWE token carrying
``{"k": <opaque key>}`` plus an ``iat`` / ``exp``. Upload has no capability to
sign: ``create`` *is* the upload, so this module is download-only.

Two facts drive the verifier:

* **``decrypt_jwe_token`` does not enforce ``exp``.** The expiry check is the
  *caller's* responsibility, so :func:`verify_signed_url` rejects a token whose
  ``exp`` has passed -- without it a capability URL would be valid forever.
* **The token is a bearer capability.** Anyone holding the URL can use it until
  it expires, so it is short-lived and bound to exactly one key. AES-GCM
  authenticates the claim, so a client cannot forge or edit it, but it can
  *replay* it, which the TTL bounds.

:class:`SignedFileStore` owns the mint / verify helpers over an injected
:class:`~resourcey.encryption.encryption_service.EncryptionService`, so its
concrete subclasses implement only the medium operations.

This module imports no code outside the framework.
"""

from __future__ import annotations

from abc import ABC
from datetime import datetime, timedelta
from urllib.parse import quote

from pydantic import PrivateAttr

from resourcey.core.errors import InvalidInputError
from resourcey.encryption.encryption_service import (
    EncryptionService,
    get_encryption_service,
    utc_now,
)
from resourcey.filestore.file_store import FileStore, PresignedUrl

# The route the framework-signed download method is served from. The
# ``{key}`` placeholder is substituted with the (already URL-safe) opaque key.
DEFAULT_SIGNED_URL_PATH = "/_files/{key}"


def signed_url_path(key: str, *, path_template: str = DEFAULT_SIGNED_URL_PATH) -> str:
    """The route path for ``key`` (quoted so an opaque key stays one segment)."""
    return path_template.replace("{key}", quote(key, safe=""))


def mint_signed_url(
    encryption_service: EncryptionService,
    key: str,
    *,
    expires_in_seconds: int,
    base_url: str = "",
    path_template: str = DEFAULT_SIGNED_URL_PATH,
) -> PresignedUrl:
    """Mint a framework-signed ``GET`` capability URL for one object key.

    ``base_url`` is prepended when set; otherwise the URL is relative to the API
    host serving it.
    """
    token = encryption_service.create_jwe_token(
        {"k": key},
        expires_in=timedelta(seconds=expires_in_seconds),
    )
    url = (
        f"{base_url.rstrip('/')}{signed_url_path(key, path_template=path_template)}"
        f"?token={quote(token)}"
    )
    return PresignedUrl(
        url=url,
        method="GET",
        expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
    )


def verify_signed_url(
    encryption_service: EncryptionService,
    token: str,
    *,
    now: datetime | None = None,
) -> str:
    """Verify a capability token and return the key it authorizes.

    Rejects a malformed / tampered token, one whose ``exp`` has passed (the codec
    does not enforce it), and one missing its ``k`` claim. Comparing the key
    against the request path is the route's job.
    """
    try:
        claims = encryption_service.decrypt_jwe_token(token)
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidInputError(f"Invalid signed URL: {exc}") from exc

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
    if current.timestamp() >= float(exp):  # type: ignore[arg-type]
        raise InvalidInputError("Signed URL has expired")


class SignedFileStore(FileStore, ABC):
    """A medium whose download capability is framework-signed and served by the API.

    ``presign_get`` is concrete here (it only mints a JWE); a subclass supplies
    the medium operations plus the URL shape.
    """

    signed_url_base_url: str = ""
    signed_url_path_template: str = DEFAULT_SIGNED_URL_PATH

    _encryption: EncryptionService | None = PrivateAttr(default=None)

    def encryption_service(self) -> EncryptionService:
        """The service that signs and verifies this store's capabilities."""
        if self._encryption is None:
            self._encryption = get_encryption_service()
        return self._encryption

    def presign_get(self, key: str, *, expires_in_seconds: int) -> PresignedUrl:
        return mint_signed_url(
            self.encryption_service(),
            key,
            expires_in_seconds=expires_in_seconds,
            base_url=self.signed_url_base_url,
            path_template=self.signed_url_path_template,
        )

    def verify(self, token: str) -> str:
        """Verify a capability token; return the key it authorizes."""
        return verify_signed_url(self.encryption_service(), token)
