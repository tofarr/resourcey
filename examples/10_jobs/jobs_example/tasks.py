"""A scheduled background task that *enqueues* a durable job.

This is the bridge between the two frameworks: :mod:`resourcey.tasks` runs
in-process cron work, while :mod:`resourcey.jobs` runs durable, claimable work.
A scheduled task is a natural **producer** — every tick it enqueues a durable job
rather than doing the work inline, so the work survives a crash, is retried, and
is claimed by exactly one worker.

The task's payload is its config: :class:`EnqueueEchoConfig` extends the base
:class:`~resourcey.tasks.task.BackgroundTaskConfig` with the ``message`` to echo,
parsed from ``APP_BACKGROUND_TASKS_<n>_MESSAGE``. The task holds the
:class:`~resourcey.jobs.jobs_runner.JobRunner` in a
:class:`~pydantic.PrivateAttr` (runtime wiring, not config).
"""

from __future__ import annotations

from pydantic import Field, PrivateAttr

# Importing the kind here guarantees it is registered (the discriminator lookup
# needs the concrete class to have been imported once) before any job is
# deserialized.
from jobs_example.jobs import EchoJobDetails
from resourcey.jobs.jobs_runner import JobRunner
from resourcey.tasks.task import BackgroundTask, BackgroundTaskConfig


class EnqueueEchoConfig(BackgroundTaskConfig):
    """Config for :class:`EnqueueEchoTask`: the message to echo each tick."""

    message: str = Field(default="scheduled echo", description="The text to echo each tick.")


class EnqueueEchoTask(BackgroundTask):
    """On each tick, enqueue an :class:`~jobs_example.jobs.EchoJobDetails` job."""

    config: EnqueueEchoConfig
    _runner: JobRunner = PrivateAttr()

    def __init__(self, config: EnqueueEchoConfig, *, runner: JobRunner) -> None:
        super().__init__(config=config)
        self._runner = runner

    async def __call__(self) -> None:
        await self._runner.enqueue(EchoJobDetails(message=self.config.message))
