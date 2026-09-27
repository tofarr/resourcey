"""The API-key resources and key-generation helpers (issue #118).

Two key sources, declared once with **full storage truth** and exposed through a
narrowing :class:`~resourcey.v2.view.resource_view.ResourceView`:

* the **DB-backed** resource — an ORM ``ApiKey`` row minted / revoked through the
  ordinary REST surface (``POST /api-keys``, ``GET /api-keys``,
  ``PATCH /api-keys/{id}``, ``DELETE /api-keys/{id}``);
* the **config-list** resource — a read-only :class:`ListResource` built from
  :class:`~resourcey.v2.auth.auth_config.ApiKeysConfig`.

Both store the key **only as a SHA-256 digest**. The ORM attribute is ``key``
(mapped to the column ``key_hash``) and the declared annotation is
:class:`~pydantic.SecretStr`, so the model layer treats it as a secret even
though the stored value is already a digest. A presented key is validated by
hashing it and searching for that hash — never through the public query surface,
which the view closes (``?key__eq=`` / ``?sort=key`` are rejected).

The raw key is disclosed exactly once, in the ``201`` create response, through
the ``expose_secrets`` serialization context (see
:mod:`resourcey.v2.auth.auth_api_key_service`). Every other surface is free of
it.

The exposed view drops ``key`` from the read / search / update models and the
create request, keeping it only in the create response. ``in_create_request``
must be ``False``: it defaults to ``True``, and without the override a client
could supply the digest the lookup would then match.

This module is part of ``v2/auth``: it imports only ``v2``.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.auth.auth_config import ApiKeysConfig
from resourcey.v2.core.resource import Resource
from resourcey.v2.list.list_resource import ListResource
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.view.resource_view import ResourceView

# The name of the secret-bearing field, shared with the service that reveals it.
KEY_FIELD = "key"

API_KEY_RANDOM_BITS = 256

# The preset pattern every key follows: a prefix that makes a leaked key
# recognisable as a resourcey key (so secret scanners can match it) plus the
# random secret.
API_KEY_FORMAT = "rsk_{secret}"

_BASE36_ALPHABET = string.digits + string.ascii_lowercase

# ceil(API_KEY_RANDOM_BITS / log2(36)). Every secret is padded to this width so
# a key's length never varies with the value it encodes.
_BASE36_WIDTH = 50

# SHA-256 hex is 64 characters.
_KEY_HASH_LENGTH = 64

# The projection both exposed views share: the key is never read, searched,
# updated, or client-supplied — only the create response reveals it.
KEY_EXPOSED: dict[str, dict[str, Any]] = {
    KEY_FIELD: {
        "in_create_request": False,
        "in_create_response": True,
        "in_read_response": False,
        "in_search_response": False,
        "in_update_request": False,
        "in_update_response": False,
    }
}

# The read-only variant: the key is hidden from every response and the query
# surface, and ``principal_id`` / ``roles`` (internal credential bindings, not
# part of the public listing) are hidden too. A read-only resource has no create
# route, so there is nothing to reveal; leaving the create flags untouched is
# therefore inert, but an explicit hide is clearer than relying on the action
# set.
KEY_QUERY_SURFACE_HIDDEN: dict[str, dict[str, Any]] = {
    KEY_FIELD: {"in_read_response": False, "in_search_response": False},
    "principal_id": {"in_read_response": False, "in_search_response": False},
    "roles": {"in_read_response": False, "in_search_response": False},
}

# The DB-backed view is the full key surface: only the secret ``key`` is
# projected away (``KEY_EXPOSED``). ``roles`` stays a normal writable field — a
# credential-carried role is an entry on the key's definition, settable (and
# rotatable) through the REST surface, not a secret. Note the consequence: the
# DB-backed key resource is therefore a **privilege-assignment surface** —
# whoever can write a key row can grant it roles — so access to this resource
# must itself be restricted to administrators.
STORED_KEY_EXPOSED: dict[str, dict[str, Any]] = dict(KEY_EXPOSED)


def encode_base36(value: int, width: int) -> str:
    """Render ``value`` in base 36, zero-padded to ``width`` characters."""
    digits: list[str] = []
    while value > 0:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits)).rjust(width, _BASE36_ALPHABET[0])


def generate_api_key(key_format: str = API_KEY_FORMAT) -> str:
    """Mint a key: a fresh random secret rendered into ``key_format``.

    The secret is :data:`API_KEY_RANDOM_BITS` bits from
    :func:`secrets.token_bytes` (a CSPRNG), so keys are unguessable and
    collisions are not a practical concern.
    """
    secret = int.from_bytes(secrets.token_bytes(API_KEY_RANDOM_BITS // 8), "big")
    return key_format.format(secret=encode_base36(secret, _BASE36_WIDTH))


def hash_api_key(raw: str) -> str:
    """The SHA-256 hex digest of a raw key — the value stored at rest.

    Hash-and-search is the validation strategy: a presented key is hashed and
    the digest looked up, so neither the table nor the config list holds a usable
    credential.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    """Default factory for the ``created_at`` / ``updated_at`` columns."""
    return datetime.now(UTC)


class ApiKeyBase(DeclarativeBase):
    """The declarative base owning the ``api_keys`` table.

    Kept local so the auth package is self-contained; an app can point Alembic at
    this metadata (or import the model) exactly as it does for any SQLAlchemy
    model.
    """


class ApiKey(ApiKeyBase):
    """An API key stored as a row, mintable and revocable at runtime.

    The ORM attribute is ``key``; the DB column is ``key_hash`` and stores the
    SHA-256 digest. Declared as :class:`~pydantic.SecretStr` so the derived DTO
    field is a secret (redacted / encrypted / revealed per the serialization
    context) even though the stored value is already a digest.

    Fields:
        id: Primary key, generated as a UUID4.
        name: Optional human-readable label (the only editable field).
        key: The secret digest. Generated on create, never accepted from a
            client, never updated, and absent from every read model — so the
            create response is the only place it is disclosed, and even there it
            is the *raw* key (the service reveals the minted value).
        created_at: Set on create.
        updated_at: Set on create and re-set on update.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key: Mapped[SecretStr] = mapped_column(
        "key_hash", String(_KEY_HASH_LENGTH), nullable=False, unique=True, index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class StoredApiKeyResource(SqlResource[Any, Any]):
    """The DB-backed key resource: the ``ApiKey`` model, with ``find_by_key``.

    A thin :class:`~resourcey.v2.sql.sql_resource.SqlResource` subclass that
    builds a :class:`~resourcey.v2.auth.auth_api_key_service.StoredApiKeyService`
    (which adds ``find_by_key`` and the one-time reveal). The service import is
    deferred to :meth:`make_service` so this module and the service module do not
    import one another at module load.
    """

    def make_service(self, ctx: Any, session_factory: Any) -> Any:
        from resourcey.v2.auth.auth_api_key_service import StoredApiKeyService

        return StoredApiKeyService(self, ctx, session_factory)


class ConfigApiKeyResource(ListResource[Any, Any]):
    """The read-only config key resource, with ``find_by_key``.

    A thin :class:`~resourcey.v2.list.list_resource.ListResource` subclass that
    builds a :class:`~resourcey.v2.auth.auth_api_key_service.ConfigApiKeyService`
    (which adds ``find_by_key``).
    """

    def make_service(self, ctx: Any, items: list[Any]) -> Any:
        from resourcey.v2.auth.auth_api_key_service import ConfigApiKeyService

        return ConfigApiKeyService(self, ctx, items)


def stored_api_key_resource(
    *,
    session_factory: Any = None,
    session_manager: Any = None,
    name: str | None = None,
    path: str | None = None,
) -> Resource[Any, Any]:
    """The DB-backed key resource: the ``ApiKey`` ORM model with full storage truth.

    The returned resource is the **inner** resource — the authenticator holds it
    to reach :meth:`~resourcey.v2.auth.auth_api_key_service.StoredApiKeyService.find_by_key`.
    Register :func:`stored_api_key_view` in the manifest, not this.
    """
    return StoredApiKeyResource(
        ApiKey,
        session_factory=session_factory,
        session_manager=session_manager,
        name=name,
        path=path or "api-keys",
    )


def stored_api_key_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed view over a DB-backed key resource (the one-time reveal)."""
    return ResourceView(inner, exposed_field_overrides=STORED_KEY_EXPOSED)


class ConfigApiKey(BaseModel):
    """A served config key: the identifier, the label, the digest, and its roles.

    Only ``id`` / ``name`` are ever exposed; ``key`` holds the SHA-256 digest and
    is hidden by the read-hiding view, so it can be searched by the authenticator
    but never read. ``principal_id`` (an optional fixed principal for the key)
    and ``roles`` (the roles the key authenticates as) are carried so the
    authenticator can resolve them without a DB lookup.
    """

    id: str
    name: str | None = None
    key: SecretStr
    principal_id: str | None = None
    roles: list[str] = Field(default_factory=list)


def config_api_key_models(config: ApiKeysConfig) -> list[ConfigApiKey]:
    """The served config entries, with each plaintext key hashed on load.

    The plaintext (:class:`~pydantic.SecretStr`) is read once, hashed, and not
    retained: the returned entries hold only the digest.
    """
    return [
        ConfigApiKey(
            id=entry.id,
            name=entry.name,
            key=SecretStr(hash_api_key(entry.key.get_secret_value())),
            principal_id=entry.principal_id,
            roles=list(entry.roles),
        )
        for entry in config.api_keys
    ]


def config_api_key_resource(
    config: ApiKeysConfig, *, path: str | None = None
) -> ConfigApiKeyResource:
    """The read-only, config-backed key resource over the hashed entries."""
    return ConfigApiKeyResource(
        config_api_key_models(config),
        model=ConfigApiKey,
        path=path or "api-keys",
    )


def config_api_key_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed view over a config-list key resource (read-only, key hidden).

    Uses :data:`KEY_QUERY_SURFACE_HIDDEN` (not :data:`KEY_EXPOSED`): the config
    list is read-only, so there is no create request to close and no create
    response to reveal — the key is simply absent from every response and from
    the query surface.
    """
    return ResourceView(inner, exposed_field_overrides=KEY_QUERY_SURFACE_HIDDEN)
