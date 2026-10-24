"""``JobDetails`` — the polymorphic, stored job body (issue #16).

A :class:`JobDetails` is a ``DiscriminatedUnionMixin`` async callable: a concrete
kind carries its own pydantic fields (serialized to the ``jobs.job_details`` JSON
column) and implements ``async def __call__(self) -> JobRun``. An app declares its
own kinds; the framework ships one trivial reference kind, ``LogJobDetails``.

A stored job is therefore **data that names its own behavior**: the row keeps the
discriminator (``job_details_kind``) and the serialized details, and the runner
deserializes them back to the concrete subclass before invoking it. A kind only
needs to be imported once for deserialization to find it.

This module is part of ``resourcey.jobs``; it imports only lower framework
layers (``util``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

from resourcey.util.models import DiscriminatedUnionMixin

logger = logging.getLogger(__name__)

# The terminal outcomes a job body may report; a raised exception maps to
# ``ERROR`` too.
JobOutcome = Literal["COMPLETED", "ERROR"]


class JobRun(BaseModel):
    """The terminal outcome a :class:`JobDetails` reports when it finishes.

    A body that raises is treated as ``ERROR`` by the runner.
    """

    status: JobOutcome = "COMPLETED"
    detail: str | None = None


class JobDetails(DiscriminatedUnionMixin, ABC):
    """An async, self-describing unit of work — the stored job body.

    Subclasses declare whatever pydantic fields their work needs and implement
    :meth:`__call__`.
    """

    @abstractmethod
    async def __call__(self) -> JobRun:
        """Run the job's work and report its terminal outcome.

        The runner invokes this with **no database session held open**. Raising
        is allowed: the runner catches it and records ``ERROR`` (or requeues).
        """


class LogJobDetails(JobDetails):
    """The framework's trivial reference kind: log a message and succeed.

    It lets an app enqueue a real job with no custom kind, and gives the
    framework a concrete kind for tests and examples.
    """

    message: str = Field(description="The text to log when the job runs.")

    async def __call__(self) -> JobRun:
        logger.info("LogJobDetails: %s", self.message)
        return JobRun(status="COMPLETED", detail=self.message)
