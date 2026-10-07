"""The example's job kinds — the polymorphic work bodies.

Each kind subclasses :class:`~resourcey.jobs.jobs_details.JobDetails`, carries
whatever pydantic fields its work needs, and implements ``async def __call__``
returning a :class:`~resourcey.jobs.jobs_details.JobRun`. The body is stored as
JSON in the ``jobs.job_details`` column alongside its ``kind`` discriminator, so
the runner deserializes it back to the concrete class and invokes it — the
process only has to have imported this module once (``app.py`` imports it) for
the kinds to resolve.

Three kinds demonstrate the interesting cases:

* :class:`EchoJobDetails` — succeeds, reporting a value as the row's ``detail``.
* :class:`SlowJobDetails` — sleeps, showing the runner holds **no** database
  session while a body runs and runs bodies concurrently.
* :class:`BoomJobDetails` — always raises, showing the retry-then-terminal-ERROR
  path (a real app's transient failure would eventually succeed).
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import Field

from resourcey.jobs.jobs_details import JobDetails, JobRun

logger = logging.getLogger(__name__)


class EchoJobDetails(JobDetails):
    """Log a message and succeed, echoing it back as the job's ``detail``."""

    message: str = Field(description="The text to log and echo.")

    async def __call__(self) -> JobRun:
        logger.info("EchoJobDetails: %s", self.message)
        return JobRun(status="COMPLETED", detail=f"echoed: {self.message}")


class SlowJobDetails(JobDetails):
    """Sleep for a while, then succeed — a long-running body.

    It proves the runner holds no session across the body (the sleep would
    otherwise pin a connection) and that several bodies run concurrently.
    """

    seconds: float = Field(default=1.0, description="How long to sleep before succeeding.")

    async def __call__(self) -> JobRun:
        logger.info("SlowJobDetails: sleeping %ss", self.seconds)
        await asyncio.sleep(self.seconds)
        return JobRun(status="COMPLETED", detail=f"slept {self.seconds}s")


class BoomJobDetails(JobDetails):
    """Always raise — the failure / retry / terminal-ERROR path.

    Enqueue it with ``max_attempts > 1`` to watch the runner requeue it and then
    give up at the cap.
    """

    message: str = Field(default="boom", description="The text to log before raising.")

    async def __call__(self) -> JobRun:
        logger.warning("BoomJobDetails: raising (%s)", self.message)
        raise RuntimeError(self.message)
