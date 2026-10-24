"""``TriggerRunner`` — the app-scoped home for background trigger runs (issue #155).

A ``TriggeredService`` is built **fresh per request**, and Starlette closes a
``yield`` dependency synchronously right after the handler returns — well before
any real webhook latency could elapse. Tracking a ``background=True`` run on that
per-request instance and cancelling it from its ``__aexit__`` would therefore
cancel the task before the event loop ever gives it a turn.

``TriggerRunner`` is the fix: a small, shared object owned by something
**app-scoped** — a ``TriggeredResource`` instance, or a
``TriggeredDependencyBuilder`` added to ``Manifest(managers=[...])`` — and handed
to every per-request ``TriggeredService`` that fires through it. A background run
outlives the request that started it and is cancelled only at app shutdown.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers, plus its sibling ``trigger``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults

logger = logging.getLogger(__name__)


class TriggerRunner:
    """Fires configured triggers and tracks background runs at the right scope.

    Create one instance per app-scoped owner and share it across every
    per-request ``TriggeredService``; call :meth:`aclose` from the *owner's*
    shutdown, never from a per-request service's exit.
    """

    def __init__(self) -> None:
        self._in_flight: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> frozenset[asyncio.Task[None]]:
        """The currently tracked background tasks (read-only; for tests/introspection)."""
        return frozenset(self._in_flight)

    async def fire(
        self,
        triggers: Sequence[Trigger[Any, Any]],
        edits: TriggerEdits[Any, Any],
        results: TriggerResults[Any],
        *,
        background: bool,
    ) -> None:
        """Fire every trigger once, in the background or inline."""
        if not triggers:
            return
        if background:
            task = asyncio.create_task(self._run_all(triggers, edits, results))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        else:
            await self._run_all(triggers, edits, results)

    async def _run_all(
        self,
        triggers: Sequence[Trigger[Any, Any]],
        edits: TriggerEdits[Any, Any],
        results: TriggerResults[Any],
    ) -> None:
        """Invoke every trigger, isolating each in its own catch block."""
        outcomes = await asyncio.gather(
            *(trigger.callback(edits, results) for trigger in triggers),
            return_exceptions=True,
        )
        for trigger, outcome in zip(triggers, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.exception(
                    "Trigger %s raised; the remaining triggers still ran.",
                    type(trigger).__name__,
                    exc_info=outcome,
                )

    async def aclose(self) -> None:
        """Cancel and await any still-running background runs (idempotent)."""
        pending = [*self._in_flight]
        self._in_flight.clear()
        for running in pending:
            running.cancel()
        for running in pending:
            with suppress(asyncio.CancelledError):
                await running
