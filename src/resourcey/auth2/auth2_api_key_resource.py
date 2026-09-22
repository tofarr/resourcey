"""The database-backed ``ApiKey`` resource (issue #63).

:mod:`resourcey.auth2.auth2_api_key` holds the simplest posture: a fixed list
of keys read from the environment. This module is the next rung up — keys are
rows, so they can be minted, named, listed, and revoked at runtime through the
ordinary REST surface the framework generates for any resource
(``POST /api-keys``, ``GET /api-keys``, ``PATCH /api-keys/{id}``,
``DELETE /api-keys/{id}``).

The key value itself is never supplied by a client and never changes: it is
generated from :data:`API_KEY_FORMAT`, which carries a
:data:`API_KEY_RANDOM_BITS`-bit random secret rendered in base 36. Declaring
the field ``creatable=False`` / ``updatable=False`` / ``readable=False`` is
what enforces that — the generated create and update models have no ``key``
field (so a client cannot set or rotate one), and the read model has none
either, so no read, search, or batch response can disclose a stored key.
Because the query surface is derived from the read model, ``?sort=key`` and
``?key__eq=`` are rejected too: a sortable or filterable hidden field leaks
its relative order or its value without ever appearing in a response body.

The plaintext key is therefore visible exactly once, in the ``201`` response
to the create that minted it. That one-time reveal is the job of
:class:`~resourcey.auth2.auth2_api_key_service.ApiKeyService`, which this
resource selects in :meth:`ApiKey.build_service`.

Security trade-off: the column stores the key in plaintext, so read access to
the table is equivalent to holding every key. It is stored in full (rather
than as a one-way digest) because a later phase authenticates a presented key
by looking the row up; when that lands, switching the lookup to a digest
column is the hardening step, and it is confined to this resource plus its
authenticator.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import Column, String

from resourcey.resource.base import BaseResource
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from resourcey.util import utc_now

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

# Wide enough for the default format and any reasonable custom one.
_KEY_COLUMN_LENGTH = 255


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


class ApiKey(SqlResource):
    """An API key stored as a row, mintable and revocable at runtime.

    Fields:
        id: Primary key, generated client-side as a UUID4.
        name: Optional human-readable label (the only editable field).
        key: The secret. Generated on create, never accepted from a client,
            never updated, and absent from every read model — so it is
            disclosed only in the create response.
        created_at: Set on create.
        updated_at: Set on create.
    """

    id: UUID = Field(default_factory=uuid4)
    name: str | None = None
    key: Annotated[
        str,
        # A default_factory (rather than the service alone) so a key minted
        # through the plain repository escape hatch is still a real key and
        # never NULL.
        Field(default_factory=generate_api_key),
        ResourceyField(
            creatable=False,
            updatable=False,
            readable=False,
            column=Column(
                KEY_FIELD,
                String(_KEY_COLUMN_LENGTH),
                nullable=False,
                unique=True,
                index=True,
            ),
        ),
    ]
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def build_service(self, resource: BaseResource, storage: Any) -> Any:
        """Build the service that reveals a freshly minted key once."""
        from resourcey.auth2.auth2_api_key_service import ApiKeyService

        return ApiKeyService(resource, session=storage)
