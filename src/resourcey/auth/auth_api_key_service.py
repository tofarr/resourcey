"""The services backing the two API-key resources (issue #118).

Both expose the same lookup seam a presented key is validated through::

    async def find_by_key(self, key_hash: str) -> Any | None

``key_hash`` is the SHA-256 digest of the presented key; a match is the stored
entry whose digest equals it. The lookup is a dedicated method, never a
:class:`~resourcey.util.search_filter.SearchFilter` — the public query surface
is closed over the key (the exposed view removes it), so a guess-confirmation
oracle over the digest must not be reachable through ``?key__eq=``.

* :class:`StoredApiKeyService` mints a raw key, stores its digest, and reveals
  the raw value once through the ``expose_secrets`` serialization context. Its
  ``find_by_key`` is an indexed equality lookup through the resource's SQL
  escape hatch.
* :class:`ConfigApiKeyService` looks the digest up in the (small, in-process)
  list, comparing with :func:`secrets.compare_digest`.

This module is part of ``resourcey.auth``; it imports only lower framework layers.
"""

from __future__ import annotations

import secrets
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr

from resourcey.auth.auth_api_key_resource import KEY_FIELD, generate_api_key, hash_api_key
from resourcey.list.list_service import ListService
from resourcey.sql.sql_service import SqlService

# The context that makes the create response disclose the freshly minted key.
EXPOSE_SECRETS: dict[str, Any] = {"expose_secrets": True}


@runtime_checkable
class SupportsFindByKey(Protocol):
    """A service that can look a presented key's digest up without the query surface."""

    async def find_by_key(self, key_hash: str) -> Any | None:
        """Return the entry whose stored digest is ``key_hash``, or ``None``."""
        ...


class StoredApiKeyService(SqlService[Any, Any]):
    """A :class:`SqlService` whose ``create`` mints, stores a digest, and reveals.

    The stored value is always the SHA-256 digest of a freshly minted key; the
    raw key is disclosed exactly once, in the create response, via the
    serialization context. Every other action is inherited (and the key is hidden
    from every read model), so the digest never leaves.
    """

    async def create(self, payload: Any) -> Any:
        """Mint a key, persist its digest, and return the DTO with the raw key.

        The returned DTO's ``key`` is the **raw** minted value, and the
        serialization context is set to ``expose_secrets`` so the transport
        serializes it in the ``201`` body. This is the only response that carries
        it — a caller that loses it must mint a replacement.
        """
        raw = generate_api_key()
        stored = payload.model_copy(update={KEY_FIELD: SecretStr(hash_api_key(raw))})
        created = await super().create(stored)
        self.set_serialization_context(EXPOSE_SECRETS)
        return created.model_copy(update={KEY_FIELD: SecretStr(raw)})

    async def find_by_key(self, key_hash: str) -> Any | None:
        """Look the digest up by indexed equality; ``None`` when absent.

        A direct ``WHERE key_hash = :digest`` through the resource's escape
        hatch (``table`` / the mapped ``key`` column), never a ``SearchFilter``,
        so the lookup is not reachable from the public query surface.
        """
        session = self._active_session()
        column = self._resource.table.c[self._resource.get_column_name(KEY_FIELD)]
        statement = column.table.select().where(column == key_hash)
        row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        return self._to_dto(row)


class ConfigApiKeyService(ListService[Any, Any]):
    """A :class:`ListService` that can look a digest up in the served list.

    The served items are the config entries with each plaintext hashed on load,
    so ``find_by_key`` scans the (small, in-process) digests and compares with
    :func:`secrets.compare_digest`.
    """

    async def find_by_key(self, key_hash: str) -> Any | None:
        """Return the entry whose stored digest equals ``key_hash``, or ``None``.

        The comparison is constant-time for the presented digest, so a candidate
        whose digest shares a prefix with a stored one does not leak on timing.
        """
        self._require_entered()
        candidate = key_hash.encode()
        for item in self._items:
            stored = getattr(item, KEY_FIELD, None)
            raw = stored.get_secret_value() if isinstance(stored, SecretStr) else stored
            if raw is not None and secrets.compare_digest(candidate, str(raw).encode()):
                return item
        return None
