"""``BackgroundTaskScheduler`` — the task manager (issue #15).

The scheduler is an ordinary ``Manifest`` **manager**: it is entered through the
existing ``managers=`` slot (like
:class:`~resourcey.sql.session_manager.SqlSessionManager` /
:class:`~resourcey.mongo.mongo_client.MongoClientManager`), so ``core`` stays
unaware of tasks and the whole feature lives in its own package.

Entering starts **one** asyncio loop that fires each *active*, scheduled task on
its cron schedule; leaving cancels it. The loop imposes **no mutual exclusion** —
a schedule whose interval is shorter than a run simply overlaps, so a task that
must not overlap enforces that itself (e.g. a Redis lock). A raising task is
caught and logged, and the loop continues; there are no retries.

Entering is gated by ``background_tasks_scheduler_enabled``, so a web replica or
a test process can start the manifest without scheduling anything. A one-shot
command instead calls :meth:`run_once` **without** entering the loop, so it can
run a task by name while the periodic schedule stays dormant.

This module imports only lower framework layers (``core`` / ``config`` / ``util``
and the sibling task / cron modules).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from resourcey.core.manifest import Manifest
from resourcey.tasks.cron import CronSchedule, parse_cron
from resourcey.tasks.task import (
    BackgroundTask,
    BackgroundTasksConfig,
    assert_unique_names,
)

logger = logging.getLogger(__name__)


class BackgroundTaskScheduler:
    """Runs a set of background tasks on their cron schedules.

    Args:
        tasks: The tasks to schedule. Names must be unique (checked here, the
            scheduler being the one place that sees them all).
        config: The central config; defaults to
            :meth:`BackgroundTasksConfig.get_instance`, which supplies the
            timezone, the scheduler toggle, and (when the app has not already
            wired it) the per-task entries.
        timezone: An explicit timezone override (mostly for tests). When
            ``None``, the config's ``background_tasks_timezone`` is used.
    """

    def __init__(
        self,
        tasks: list[BackgroundTask],
        *,
        config: BackgroundTasksConfig | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        assert_unique_names(tasks)
        self._tasks: list[BackgroundTask] = list(tasks)
        self._by_name: dict[str, BackgroundTask] = {t.name: t for t in self._tasks}
        self._config = config if config is not None else BackgroundTasksConfig.get_instance()
        self._timezone = timezone or ZoneInfo(self._config.background_tasks_timezone)
        # A schedule that will not parse is a startup error: it is better to fail
        # here than to have a task silently never fire.
        self._schedules: dict[str, CronSchedule] = {
            task.name: parse_cron(task.schedule) for task in self._tasks if task.schedule
        }
        self._loop: asyncio.Task[None] | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._entered = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> tuple[BackgroundTask, ...]:
        """The registered tasks, in declaration order."""
        return tuple(self._tasks)

    @property
    def entered(self) -> bool:
        """Whether the scheduler is currently inside its ``async with`` block."""
        return self._entered

    def get_task(self, name: str) -> BackgroundTask:
        """The task named ``name``; raises :class:`KeyError` when there is none."""
        try:
            return self._by_name[name]
        except KeyError:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            raise KeyError(f"No background task named {name!r}; known tasks: {known}") from None

    # ------------------------------------------------------------------
    # Lifecycle (the scheduler is the async context manager)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> BackgroundTaskScheduler:
        """Mark entered and, when enabled, start the scheduling loop."""
        if self._entered:
            raise RuntimeError("BackgroundTaskScheduler is already entered")
        self._entered = True
        if self._config.background_tasks_scheduler_enabled:
            self._loop = asyncio.create_task(self._run_loop())
        else:
            logger.info(
                "Background-task scheduler is disabled; %d task(s) registered but not scheduled.",
                len(self._tasks),
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Cancel the loop and any in-flight runs, then clear entered."""
        self._entered = False
        loop, self._loop = self._loop, None
        pending = [*self._in_flight]
        self._in_flight.clear()
        for running in [loop, *pending]:
            if running is not None:
                running.cancel()
        for running in [loop, *pending]:
            if running is not None:
                with suppress(asyncio.CancelledError):
                    await running

    # ------------------------------------------------------------------
    # One-shot runs (the entry point's path; no loop needed)
    # ------------------------------------------------------------------

    async def run_once(self, name: str | None = None) -> None:
        """Run one task by name, or every enabled task, exactly once.

        A name is the explicit "run this one" override and runs the task even
        when it is disabled; with no name, every *enabled* task runs. No loop
        is started, so this is safe to call on an un-entered scheduler.
        """
        if name is not None:
            targets = [self.get_task(name)]
        else:
            targets = [task for task in self._tasks if task.enabled]
        for task in targets:
            await self._invoke(task)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Sleep until the earliest next fire, tick, and repeat."""
        while True:
            now = datetime.now(self._timezone)
            upcoming = self._upcoming(now)
            if not upcoming:
                logger.info("Background-task scheduler has no active scheduled tasks; idling.")
                return
            due_at = min(when for when, _ in upcoming)
            await asyncio.sleep(max(0.0, (due_at - now).total_seconds()))
            self.tick(datetime.now(self._timezone))

    def _upcoming(self, now: datetime) -> list[tuple[datetime, BackgroundTask]]:
        """Each active scheduled task paired with its next fire after ``now``."""
        return [
            (schedule.next_fire(now), task)
            for task in self._tasks
            if task.enabled and (schedule := self._schedules.get(task.name)) is not None
        ]

    def tick(self, now: datetime) -> None:
        """Start each active scheduled task whose schedule matches ``now``'s minute.

        Ticks are concurrent: each started task runs on its own asyncio task and
        is tracked so exit can cancel it. The loop calls this after sleeping to
        the next fire; tests call it directly with a chosen instant.
        """
        fired = now.replace(second=0, microsecond=0)
        for task in self._tasks:
            if not task.enabled:
                continue
            schedule = self._schedules.get(task.name)
            if schedule is not None and schedule.matches(fired):
                self._start(task)

    def _start(self, task: BackgroundTask) -> None:
        """Launch one task concurrently, tracked so exit can cancel it."""
        running = asyncio.create_task(self._invoke(task))
        self._in_flight.add(running)
        running.add_done_callback(self._in_flight.discard)

    async def _invoke(self, task: BackgroundTask) -> None:
        """Run a task once, catching and logging any failure."""
        try:
            await task()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task %r raised; the schedule continues.", task.name)


def scheduler_from_manifest(manifest: Manifest) -> BackgroundTaskScheduler:
    """The :class:`BackgroundTaskScheduler` among ``manifest``'s managers.

    Raises :class:`ValueError` when the manifest has no scheduler, naming the
    fix (register one in ``managers=``).
    """
    for manager in manifest.managers:
        if isinstance(manager, BackgroundTaskScheduler):
            return manager
    raise ValueError(
        "No BackgroundTaskScheduler found among the manifest's managers; register one "
        "with Manifest(resources=..., managers=[..., scheduler])."
    )
