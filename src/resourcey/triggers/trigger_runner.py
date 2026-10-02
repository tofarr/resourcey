"""``TriggerRunner`` — the app-scoped home for background trigger runs (issue #155).

A :class:`~resourcey.triggers.triggered_service.TriggeredService` is built
**fresh per request**: :meth:`~resourcey.core.resource.Resource.get_service`
is awaited once per call, so every HTTP request gets its own instance, entered
and exited within that single request's dependency lifecycle
(``async with service: yield service``, in both
:class:`~resourcey.http.dependency_builder.OpenDependencyBuilder` and
:class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`'s
own wrapping dependency). Starlette closes a ``yield`` dependency's exit stack
synchronously, in the same request-handling coroutine, immediately after the
route handler returns — well before any real webhook latency could elapse.

Tracking a ``background=True`` run's in-flight task on that per-request
service instance, and cancelling it from *that instance's* ``__aexit__``
(the shape :class:`~resourcey.tasks.scheduler.BackgroundTaskScheduler` uses),
is therefore the wrong scope: it cancels the task before the event loop ever
gives it a turn to run — ``background=True`` would never actually deliver a
trigger over the standard HTTP request flow, the framework's primary
documented use case. ``BackgroundTaskScheduler``'s pattern is safe only
because *that* object is long-lived and app-scoped (entered/exited once at
process startup/shutdown); reusing it unchanged at per-request scope defeats
the whole point of firing in the background.

``TriggerRunner`` is the fix: a small, shared object owned by something
**app-scoped** —
a :class:`~resourcey.triggers.triggered_resource.TriggeredResource` instance
(entered/exited once, by the :class:`~resourcey.core.manifest.Manifest` that
registers it), or a
:class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`
(added to ``Manifest(managers=[...])`` for the same reason) — and handed to
every per-request :class:`TriggeredService` that fires through it. A
background run therefore outlives the single request that started it, and is
only cancelled when the *runner* is closed, at actual app shutdown.

This module is part of ``resourcey.triggers``; it imports only lower
framework layers (``core`` / ``util``), plus its sibling :mod:`trigger`.
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

    Create one instance per app-scoped owner (a ``TriggeredResource``, or a
    ``TriggeredDependencyBuilder`` entry) and share it across every
    per-request ``TriggeredService`` that owner hands out; call :meth:`aclose`
    from the *owner's* shutdown, never from a per-request service's exit.
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
        """Fire every trigger once, in the background or inline.

        ``background=True`` (default) schedules :meth:`_run_all` as a tracked
        task and returns immediately; ``background=False`` awaits it inline.
        """
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
        """Cancel and await any still-running background runs.

        Mirrors :meth:`~resourcey.tasks.scheduler.BackgroundTaskScheduler.__aexit__`:
        pending runs are cancelled, awaited, and ``CancelledError`` is
        suppressed. Idempotent — settling an already-settled set is a no-op.
        """
        pending = [*self._in_flight]
        self._in_flight.clear()
        for running in pending:
            running.cancel()
        for running in pending:
            with suppress(asyncio.CancelledError):
                await running
