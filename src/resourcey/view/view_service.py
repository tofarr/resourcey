"""``ViewService`` - the action-enforcing wrapper a ``ResourceView`` yields.

The inner service consults the **inner** resource's action set, which is wider
than the view's whenever the view narrows actions. The transport already narrows
the ``batch-edit`` body, but a *direct* service caller bypasses that, so this
wrapper re-asserts the view's action set on ``batch_edit``.

Every other action is forwarded verbatim.

This module imports no code outside the framework.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from resourcey.core.errors import InvalidInputError
from resourcey.core.service import (
    Action,
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


class ViewService(Service[T, K], Generic[T, K]):
    """A service proxy that forwards to ``inner`` but enforces a view's actions.

    The DTO type is nominal - the inner service returns inner DTOs, which the
    transport projects onto the view's REST models.
    """

    def __init__(self, inner: Service[T, K], actions: frozenset[Action]) -> None:
        super().__init__()
        self._inner = inner
        self._actions = actions

    # ------------------------------------------------------------------
    # Lifecycle (delegated to the inner service)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> ViewService[T, K]:
        await super().__aenter__()
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context.

        Delegated, not the wrapper's own: the inner decides a response's context
        (e.g. a one-time secret reveal), and the transport reads it off the
        service it was handed — the view.
        """
        return self._inner.serialization_context()

    # ------------------------------------------------------------------
    # Cache privacy (delegated to the inner service)
    # ------------------------------------------------------------------

    def response_is_private(self) -> bool:
        """The inner service's cache privacy.

        A projection narrows the *fields*, but whether the body is caller-scoped
        is the inner (authorized) service's fact.
        """
        return self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Standard actions (forwarded)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        return await self._inner.create(payload)

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        return await self._inner.read(id)

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        self._require_entered()
        return await self._inner.update(payload, condition=condition)

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        self._require_entered()
        return await self._inner.delete(id, condition=condition)

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Page[T]:
        self._require_entered()
        return await self._inner.search(search_filter, sort_order, cursor, limit)

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        self._require_entered()
        return await self._inner.count(search_filter)

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        self._require_entered()
        return await self._inner.batch_read(ids)

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Forward ``batch_edit`` after enforcing the view's action set.

        The inner service guards create / delete against the *inner* resource's
        actions; this re-checks all three kinds against the *view's*, so a batch
        cannot reach an action the view hides.
        """
        self._require_entered()
        for edit in edits:
            if isinstance(edit, Create) and Action.CREATE not in self._actions:
                raise InvalidInputError("batch_edit cannot create: create is not exposed")
            if isinstance(edit, Update) and Action.UPDATE not in self._actions:
                raise InvalidInputError("batch_edit cannot update: update is not exposed")
            if isinstance(edit, Delete) and Action.DELETE not in self._actions:
                raise InvalidInputError("batch_edit cannot delete: delete is not exposed")
        return await self._inner.batch_edit(edits)
