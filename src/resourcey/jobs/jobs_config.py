"""``JobsConfig`` — the durable-job runner's env surface (issue #16).

A :class:`~resourcey.config.config_base.BaseConfig` block parsed under the
process-wide prefix as ``APP_JOBS_*``, mirroring
:class:`~resourcey.tasks.task.BackgroundTasksConfig`. Field names are prefixed
(``jobs_*``) so they do not collide with another block's fields in the shared
flat namespace (``BaseConfig.__init_subclass__`` rejects a same-name /
different-type redeclaration).

This module is part of ``resourcey.jobs``; it imports only lower framework
layers (``config``).
"""

from __future__ import annotations

from pydantic import Field

from resourcey.config.config_base import BaseConfig

# How long a claimed job may run before its claim is considered stale and
# recoverable. Fail-closed: a job with no per-job ``max_seconds_for_run`` falls
# back to this, so every job always has a recovery bound and none is ever stuck.
DEFAULT_MAX_SECONDS_FOR_RUN = 3600

# The default sweep interval: how often the runner looks for claimable jobs and
# recovers stale claims.
DEFAULT_SWEEP_INTERVAL_SECONDS = 5.0

# The default cap on how many jobs one runner runs concurrently.
DEFAULT_MAX_CONCURRENT_JOBS = 4

# The default retry cap: one attempt, i.e. no retry unless a job opts in.
DEFAULT_MAX_ATTEMPTS = 1


class JobsConfig(BaseConfig):
    """The durable-job runner's configuration.

    Attributes:
        jobs_sweep_interval_seconds: How often the runner sweeps for claimable
            jobs and recovers stale claims.
        jobs_default_max_seconds_for_run: The stale-claim bound used when a job
            leaves ``max_seconds_for_run`` unset. Fail-closed, so no job is ever
            stuck ``RUNNING``.
        jobs_max_concurrent_jobs: How many jobs one runner runs concurrently.
        jobs_default_max_attempts: The retry cap used when a job leaves
            ``max_attempts`` unset; ``1`` means no retry.
        jobs_runner_enabled: Whether this process starts the sweep loop when the
            manifest is entered. A web replica sets it ``False`` so only the
            dedicated worker process runs jobs.
    """

    jobs_sweep_interval_seconds: float = Field(
        default=DEFAULT_SWEEP_INTERVAL_SECONDS,
        description="How often the runner sweeps for claimable jobs and recovers stale claims.",
    )
    jobs_default_max_seconds_for_run: int = Field(
        default=DEFAULT_MAX_SECONDS_FOR_RUN,
        description=(
            "Stale-claim bound (seconds) used when a job leaves max_seconds_for_run unset; "
            "fail-closed so every job is recoverable."
        ),
    )
    jobs_max_concurrent_jobs: int = Field(
        default=DEFAULT_MAX_CONCURRENT_JOBS,
        description="How many jobs one runner runs concurrently.",
    )
    jobs_default_max_attempts: int = Field(
        default=DEFAULT_MAX_ATTEMPTS,
        description="Retry cap used when a job leaves max_attempts unset; 1 means no retry.",
    )
    jobs_runner_enabled: bool = Field(
        default=True,
        description=(
            "Whether this process starts the job sweep loop when the manifest is entered; "
            "a web replica sets this False so only the worker process runs jobs."
        ),
    )
