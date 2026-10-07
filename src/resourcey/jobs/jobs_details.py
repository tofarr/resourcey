"""``JobDetails`` — the polymorphic, stored job body (issue #16).

A :class:`JobDetails` is a :class:`~resourcey.util.models.DiscriminatedUnionMixin`
async callable, mirroring
:class:`~resourcey.tasks.task.BackgroundTask` /
:class:`~resourcey.triggers.trigger.Trigger`: a concrete kind carries its own
pydantic fields (serialized to the ``jobs.job_details`` JSON column) and
implements ``async def __call__(self) -> JobRun``. An app declares its own kinds;
the framework ships one trivial reference kind, :class:`LogJobDetails`.

A stored job is therefore **data that names its own behavior**: the row keeps the
discriminator (``job_details_kind``, the class name) and the serialized details,
and the runner deserializes them back to the concrete subclass before invoking
it. The runner never imports an app's job kinds to do this — the
:class:`~resourcey.util.models.DiscriminatedUnionMixin` machinery routes on the
``kind`` field, so a kind only needs to be imported once (e.g. by the app module)
for deserialization to find it.

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

# The terminal outcomes a job body may report. A run either completes or errors;
# the runner maps a raised exception to ``ERROR`` too, so a kind need not catch
# everything itself.
JobOutcome = Literal["COMPLETED", "ERROR"]


class JobRun(BaseModel):
    """The terminal outcome a :class:`JobDetails` reports when it finishes.

    Attributes:
        status: ``COMPLETED`` or ``ERROR`` — the terminal status the job row
            takes. (A body that raises is treated as ``ERROR`` by the runner, so
            a kind may also simply let an exception propagate.)
        detail: Optional human-readable status text stored on the row.
    """

    status: JobOutcome = "COMPLETED"
    detail: str | None = None


class JobDetails(DiscriminatedUnionMixin, ABC):
    """An async, self-describing unit of work — the stored job body.

    Subclasses declare whatever pydantic fields their work needs (serialized to
    the ``job_details`` JSON column) and implement :meth:`__call__`. The base is
    abstract, so only concrete kinds are instantiable — the same shape as
    :class:`~resourcey.tasks.task.BackgroundTask` and
    :class:`~resourcey.triggers.trigger.Trigger`.
    """

    @abstractmethod
    async def __call__(self) -> JobRun:
        """Run the job's work and report its terminal outcome.

        The runner invokes this with **no database session held open**, so a
        long-running body never holds a connection. Raising is allowed: the
        runner catches it, logs it, and records a terminal ``ERROR`` (or
        requeues, while attempts remain).
        """


class LogJobDetails(JobDetails):
    """The framework's trivial reference kind: log a message and succeed.

    It exists so an app can enqueue a real job with no custom kind and so the
    framework has a concrete kind to exercise the round-trip / runner in tests
    and examples. Real deployments declare their own kinds.

    Attributes:
        message: The text to log when the job runs.
    """

    message: str = Field(description="The text to log when the job runs.")

    async def __call__(self) -> JobRun:
        logger.info("LogJobDetails: %s", self.message)
        return JobRun(status="COMPLETED", detail=self.message)
