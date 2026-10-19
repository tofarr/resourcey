"""``TriggeredService`` — fires edit-event triggers after a successful edit (issue #155).

Forwards every action to ``inner`` verbatim and, after a **write** action returns
successfully, invokes every configured trigger's ``callback`` once with that
operation's ``(edits, results)``. Reads never fire, and an inner exception on a
write fires nothing.

Execution policy:

* **Per-invocation isolation**: every trigger's callback runs inside its own
  ``asyncio.gather(..., return_exceptions=True)`` slot, so one raising trigger is
  caught, logged, and never stops the rest.
* **Background by default**: firing launches ``asyncio.create_task(...)`` and
  returns without awaiting, so webhook latency never blocks the edit's response.
  ``background=False`` awaits inline.
* **In-flight tracking at the right scope**: this service is built fresh **per
  request**, so firing is delegated to a ``TriggerRunner`` — shared and
  app-scoped when one is injected, owned and closed by this service otherwise.
* **Best effort, at-most-once**: no redelivery / ordering / exactly-once.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers, plus its sibling ``trigger_runner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from resourcey.core.service import (
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.trigger_runner import TriggerRunner
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


class TriggeredService(Service[T, K], Generic[T, K]):
    """A service proxy that forwards to ``inner`` and fires triggers on a successful edit.

    Firing is delegated to a ``TriggerRunner``, shared and app-scoped when one
    is injected, owned by this service otherwise.
    """

    def __init__(
        self,
        inner: Service[T, K],
        triggers: Sequence[Trigger[T, K]],
        *,
        background: bool = True,
        runner: TriggerRunner | None = None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._triggers = list(triggers)
        self._background = background
        self._owns_inner = False
        # An injected runner must not be closed here; a self-built one is owned.
        if runner is not None:
            self._runner = runner
            self._owns_runner = False
        else:
            self._runner = TriggerRunner()
            self._owns_runner = True

    @property
    def runner(self) -> TriggerRunner:
        """The :class:`TriggerRunner` this service fires through."""
        return self._runner

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TriggeredService[T, K]:
        await super().__aenter__()
        # An inner already entered stays owned by whoever opened it.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the inner service; close an owned runner, never a shared one."""
        if self._owns_runner:
            await self._runner.aclose()
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context / cache privacy (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context (delegated verbatim)."""
        return self._inner.serialization_context()

    def response_is_private(self) -> bool:
        """The inner service's cache privacy (delegated verbatim)."""
        return self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Reads (forwarded; never fire a trigger)
    # ------------------------------------------------------------------

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        return await self._inner.read(id)

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

    # ------------------------------------------------------------------
    # Writes (forwarded, then fire on success)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        result = await self._inner.create(payload)
        await self._fire([Create(item=payload)], [result])
        return result

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        self._require_entered()
        result = await self._inner.update(payload, condition=condition)
        await self._fire([Update(item=payload, condition=condition)], [result])
        return result

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        self._require_entered()
        deleted = await self._inner.delete(id, condition=condition)
        await self._fire([Delete(id=id, condition=condition)], [None])
        return deleted

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Forward ``batch_edit`` then fire each trigger once with the whole batch."""
        self._require_entered()
        results = await self._inner.batch_edit(edits)
        await self._fire(edits, results)
        return results

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    async def _fire(
        self, edits: list[Create[T] | Update[T] | Delete[K]], results: list[T | None]
    ) -> None:
        """Fire every configured trigger once, through this service's runner."""
        await self._runner.fire(self._triggers, edits, results, background=self._background)
