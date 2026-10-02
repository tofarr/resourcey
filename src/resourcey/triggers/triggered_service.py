"""``TriggeredService`` — fires edit-event triggers after a successful edit (issue #155).

The service wrapper :class:`~resourcey.triggers.triggered_resource.TriggeredResource`
yields. It forwards every action to ``inner`` verbatim and, after a **write**
action (``create`` / ``update`` / ``delete`` / ``batch_edit``) returns
successfully, invokes every configured trigger's
:meth:`~resourcey.triggers.trigger.Trigger.callback` once with that
operation's ``(edits, results)``. Reads (``read`` / ``search`` / ``count`` /
``batch_read``) never fire a trigger, and an inner exception on a write fires
none either — the trigger only sees a success.

Execution policy
----------------
* **Per-invocation isolation**: every trigger's callback runs inside its own
  ``asyncio.gather(..., return_exceptions=True)`` slot, so one raising trigger
  is caught, logged (``logger.exception``) and never stops the rest.
* **Background by default** (``background=True``): firing launches
  ``asyncio.create_task(...)`` and returns without awaiting, so webhook
  latency never blocks the edit's response. ``background=False`` awaits
  inline for reliability-critical paths.
* **In-flight tracking**: background runs are tracked
  (:class:`~resourcey.tasks.scheduler.BackgroundTaskScheduler`'s
  ``_in_flight`` pattern); on :meth:`__aexit__` they are cancelled and
  awaited, with ``CancelledError`` suppressed.
* **Best effort, at-most-once**: no redelivery / ordering / exactly-once —
  a trigger needing durability (a webhook's own retry, a pub/sub publish)
  implements that itself; the framework fires best-effort.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Generic, TypeVar

from resourcey.core.service import (
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

logger = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")


class TriggeredService(Service[T, K], Generic[T, K]):
    """A service proxy that forwards to ``inner`` and fires triggers on a successful edit.

    It is its own async context manager, delegating the storage lifetime to
    the inner service (the "whoever opens the storage owns its commit and
    close" rule is unchanged) while additionally tracking and tearing down any
    in-flight background trigger runs on exit.
    """

    def __init__(
        self,
        inner: Service[T, K],
        triggers: Sequence[Trigger],
        *,
        background: bool = True,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._triggers = list(triggers)
        self._background = background
        self._in_flight: set[asyncio.Task[None]] = set()
        self._owns_inner = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TriggeredService[T, K]:
        await super().__aenter__()
        # An inner that is already entered (e.g. a composed DependencyBuilder
        # opened it before handing it to this wrapper) stays owned by whoever
        # opened it -- adopting it here would double-enter / double-close it.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Cancel and await any in-flight background runs, then exit the inner.

        Mirrors :meth:`~resourcey.tasks.scheduler.BackgroundTaskScheduler.__aexit__`:
        pending runs are cancelled, awaited, and ``CancelledError`` is
        suppressed, so an in-flight webhook never outlives the service that
        started it.
        """
        pending = [*self._in_flight]
        self._in_flight.clear()
        for running in pending:
            running.cancel()
        for running in pending:
            with suppress(asyncio.CancelledError):
                await running
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context / cache privacy (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context (delegated verbatim).

        A trigger changes nothing observable about a response, so the
        one-time secret-reveal context an inner service supplies must survive
        wrapping unchanged.
        """
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

    async def update(self, payload: T) -> T:
        self._require_entered()
        result = await self._inner.update(payload)
        await self._fire([Update(item=payload)], [result])
        return result

    async def delete(self, id: K) -> None:  # noqa: A002
        self._require_entered()
        await self._inner.delete(id)
        await self._fire([Delete(id=id)], [None])

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Forward ``batch_edit`` then fire each trigger once with the whole batch.

        Triggers see the operation's full ``(edits, results)`` lists — one
        invocation per trigger, not one per item.
        """
        self._require_entered()
        results = await self._inner.batch_edit(edits)
        await self._fire(edits, results)
        return results

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    async def _fire(self, edits: TriggerEdits, results: TriggerResults) -> None:
        """Fire every configured trigger once, in the background or inline.

        ``background=True`` (default) schedules :meth:`_run_all` as a tracked
        task and returns immediately — the edit's response is never delayed by
        trigger latency. ``background=False`` awaits it inline instead.
        """
        if not self._triggers:
            return
        if self._background:
            task = asyncio.create_task(self._run_all(edits, results))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        else:
            await self._run_all(edits, results)

    async def _run_all(self, edits: TriggerEdits, results: TriggerResults) -> None:
        """Invoke every trigger, isolating each in its own catch block."""
        outcomes = await asyncio.gather(
            *(trigger.callback(edits, results) for trigger in self._triggers),
            return_exceptions=True,
        )
        for trigger, outcome in zip(self._triggers, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.exception(
                    "Trigger %s raised; the remaining triggers still ran.",
                    type(trigger).__name__,
                    exc_info=outcome,
                )
