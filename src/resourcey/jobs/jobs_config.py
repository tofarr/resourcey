"""``JobsConfig`` — the durable-job runner's env surface (issue #16).

A ``BaseConfig`` block parsed under the process-wide prefix as ``APP_JOBS_*``.
Field names are prefixed (``jobs_*``) so they do not collide in the shared flat
namespace.

This module is part of ``resourcey.jobs``; it imports only lower framework
layers (``config``).
"""

from __future__ import annotations

from pydantic import Field

from resourcey.config.config_base import BaseConfig

# The stale-claim bound used when a job leaves ``max_seconds_for_run`` unset.
DEFAULT_MAX_SECONDS_FOR_RUN = 3600

# How often the runner looks for claimable jobs and recovers stale claims.
DEFAULT_SWEEP_INTERVAL_SECONDS = 5.0

# The default cap on how many jobs one runner runs concurrently.
DEFAULT_MAX_CONCURRENT_JOBS = 4

# The default retry cap: one attempt, i.e. no retry unless a job opts in.
DEFAULT_MAX_ATTEMPTS = 1


class JobsConfig(BaseConfig):
    """The durable-job runner's configuration.

    ``jobs_runner_enabled`` lets a web replica start the manifest without
    running jobs, so only a dedicated worker process claims them.
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
