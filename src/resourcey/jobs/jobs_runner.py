"""``JobRunner`` — the durable, claimable-job manager (issue #16).

The runner is an ordinary :class:`~resourcey.core.manifest.Manifest` **manager**,
entered through the existing ``managers=`` slot (like
:class:`~resourcey.tasks.scheduler.BackgroundTaskScheduler` /
:class:`~resourcey.sql.session_manager.SqlSessionManager`), so ``core`` stays
unaware of jobs and the whole feature lives in its own package. On construction
it generates a **process-unique** ``runner_id``. On a sweep interval it:

1. **Recovers** stale claims and promotes due ``SCHEDULED`` jobs;
2. **Claims** up to its free capacity of ``PENDING`` jobs;
3. **Runs** each claimed job as a background asyncio task — with **no database
   session held open while the body runs**;
4. **Completes** each run via a conditional completion.

Coordination is :mod:`resourcey`'s conditional write (issue #164): both the
claim and the completion are ordinary ``Service.update(payload, condition=...)``
calls whose ``None`` result is the verdict, so the check and the write are **one
atomic operation** — no read-then-write race and no ``SELECT … FOR UPDATE SKIP
LOCKED``. A claim miss and an absent row are both ``None``, so a losing runner
cannot distinguish them. The runner writes through the **inner** ``jobs``
resource (it is not serving a request), so no authorization policy scope is
folded into its writes; the REST / ``ResourceView`` surface enforces client
scope.

Entering is gated by ``jobs_runner_enabled``, so a web replica can start the
manifest without running jobs. Tests drive the deterministic :meth:`sweep`
directly rather than waiting on the loop.

This module is part of ``resourcey.jobs``; it imports lower framework layers
(``core`` / ``config`` / ``util``).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from resourcey.core.manifest import Manifest
from resourcey.jobs.jobs_config import JobsConfig
from resourcey.jobs.jobs_details import JobDetails, JobRun
from resourcey.jobs.jobs_model import JobStatus
from resourcey.util.search_filter import (
    AttrFilter,
    EqFilter,
    SearchFilter,
    and_,
)

if TYPE_CHECKING:
    from resourcey.core.resource import Resource
    from resourcey.core.service import Service

logger = logging.getLogger(__name__)

# How many rows one internal enumeration pages through per request. A sweep
# enumerates (never a public query), so a large page keeps the sweep short.
_ENUMERATE_PAGE = 200

# A safety bound on how many claim attempts one sweep makes, so a persistent
# race (another runner winning every time) cannot spin forever.
_CLAIM_ATTEMPT_FACTOR = 4


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC.

    A SQLite ``DateTime(timezone=True)`` column round-trips as a *naive*
    datetime, so a comparison against an aware ``now`` would raise; anchoring a
    naive value to UTC keeps the sweep's age arithmetic correct on every backend.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class JobRunner:
    """Claims and runs durable ``jobs`` rows, one process-unique id per runner.

    Args:
        resource: The **inner** ``jobs`` resource (the full model). The runner
            opens its service per operation; the public
            :func:`~resourcey.jobs.jobs_model.jobs_view` is registered separately.
        config: The runner config; defaults to
            :meth:`~resourcey.jobs.jobs_config.JobsConfig.get_instance`.
        runner_id: An explicit claim id (mostly for tests); a fresh ``uuid4`` by
            default, so a restart never reuses an id and a stale claim is never
            mistaken for this process's.
        sweep_interval: Overrides the config sweep interval (seconds).
        max_concurrent: Overrides the config concurrency cap.
        default_max_seconds_for_run: Overrides the config stale-claim bound.
        default_max_attempts: Overrides the config retry cap.
        enabled: Overrides the config ``jobs_runner_enabled`` gate.
    """

    def __init__(
        self,
        resource: Resource[Any, Any],
        *,
        config: JobsConfig | None = None,
        runner_id: UUID | None = None,
        sweep_interval: float | None = None,
        max_concurrent: int | None = None,
        default_max_seconds_for_run: int | None = None,
        default_max_attempts: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._resource = resource
        self._config = config if config is not None else JobsConfig.get_instance()
        self._sweep_interval = (
            sweep_interval
            if sweep_interval is not None
            else self._config.jobs_sweep_interval_seconds
        )
        self._max_concurrent = (
            max_concurrent if max_concurrent is not None else self._config.jobs_max_concurrent_jobs
        )
        self._default_max_seconds_for_run = (
            default_max_seconds_for_run
            if default_max_seconds_for_run is not None
            else self._config.jobs_default_max_seconds_for_run
        )
        self._default_max_attempts = (
            default_max_attempts
            if default_max_attempts is not None
            else self._config.jobs_default_max_attempts
        )
        self._enabled = enabled if enabled is not None else self._config.jobs_runner_enabled
        # Process-unique: a fresh id per runner, never reused across restarts, so
        # a stale claim (from a dead process) is never mistaken for a live one.
        self.runner_id: UUID = runner_id if runner_id is not None else uuid4()
        self._loop: asyncio.Task[None] | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._running_jobs: dict[asyncio.Task[None], UUID] = {}
        self._entered = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def entered(self) -> bool:
        """Whether the runner is currently inside its ``async with`` block."""
        return self._entered

    @property
    def in_flight(self) -> int:
        """How many job bodies this runner is currently running."""
        return len(self._in_flight)

    # ------------------------------------------------------------------
    # Lifecycle (the runner is the async context manager)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> JobRunner:
        """Mark entered and, when enabled, start the sweep loop."""
        if self._entered:
            raise RuntimeError("JobRunner is already entered")
        self._entered = True
        if self._enabled:
            self._loop = asyncio.create_task(self._run_loop())
        else:
            logger.info("Job runner is disabled; no jobs will be claimed by this process.")
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Cancel the loop and drain in-flight runs, releasing their claims.

        A run cancelled mid-body leaves its row ``RUNNING``; each in-flight
        job's claim is released back to ``PENDING`` (best effort) so a graceful
        shutdown does not delay a re-run by the stale-claim bound.
        """
        self._entered = False
        loop, self._loop = self._loop, None
        running = [*self._running_jobs.items()]
        self._in_flight.clear()
        self._running_jobs.clear()
        for task, _job_id in running:
            task.cancel()
        if loop is not None:
            loop.cancel()
        for task, _job_id in running:
            with suppress(asyncio.CancelledError):
                await task
        if loop is not None:
            with suppress(asyncio.CancelledError):
                await loop
        for _task, job_id in running:
            with suppress(Exception):
                await self._release(job_id)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Sweep, sleep, repeat. A failed sweep is logged and the loop continues."""
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job runner sweep failed; the loop continues.")
            await asyncio.sleep(self._sweep_interval)

    async def sweep(self, *, now: datetime | None = None) -> int:
        """One coordination pass: recover, promote, then claim and start work.

        Returns the number of jobs this pass claimed. Deterministic and public
        so a test can drive it with a chosen ``now`` instead of waiting on the
        loop.
        """
        moment = now if now is not None else datetime.now(UTC)
        await self._recover_stale(moment)
        await self._promote_due_scheduled(moment)
        return await self._claim_and_start(moment)

    # ------------------------------------------------------------------
    # Recovery — stale claims and due scheduled jobs
    # ------------------------------------------------------------------

    async def _recover_stale(self, now: datetime) -> int:
        """Return stale ``RUNNING`` claims to ``PENDING``, or mark them ``ERROR``.

        A crashed runner leaves a job ``RUNNING`` forever; ``runner_id`` is fresh
        per process so it is never reused, and ``claimed_at`` plus the job's own
        ``max_seconds_for_run`` (falling back to the config default) is the
        bound. Under the attempt cap the job requeues; at the cap it goes
        terminal ``ERROR``.
        """
        recovered = 0
        async with await self._resource.get_service() as service:
            for job in await self._enumerate(
                service, AttrFilter(attribute="status", filter=EqFilter(value=JobStatus.RUNNING))
            ):
                if job.claimed_at is None:
                    continue
                bound = (
                    job.max_seconds_for_run
                    if job.max_seconds_for_run is not None
                    else self._default_max_seconds_for_run
                )
                age = (_as_utc(now) - _as_utc(job.claimed_at)).total_seconds()
                if age <= bound:
                    continue
                condition = self._claim_condition(job.claimed_by)
                if job.attempts < job.max_attempts:
                    payload = self._payload(
                        job.id,
                        status=JobStatus.PENDING,
                        claimed_by=None,
                        claimed_at=None,
                        detail="recovered stale claim",
                    )
                else:
                    payload = self._payload(
                        job.id,
                        status=JobStatus.ERROR,
                        claimed_by=None,
                        claimed_at=None,
                        detail="stale claim; attempts exhausted",
                    )
                if await service.update(payload, condition=condition) is not None:
                    recovered += 1
        return recovered

    async def _promote_due_scheduled(self, now: datetime) -> int:
        """Promote a due ``SCHEDULED`` job to ``PENDING`` so it becomes claimable.

        A ``SCHEDULED`` job with a ``NULL`` ``run_at`` is treated as due (it was
        scheduled without a future time). The promotion is a conditional write,
        so two runners promoting the same job is harmless.
        """
        promoted = 0
        async with await self._resource.get_service() as service:
            scheduled = await self._enumerate(
                service, AttrFilter(attribute="status", filter=EqFilter(value=JobStatus.SCHEDULED))
            )
            for job in scheduled:
                if job.run_at is not None and _as_utc(job.run_at) > _as_utc(now):
                    continue
                condition: SearchFilter[Any] = AttrFilter(
                    attribute="status", filter=EqFilter(value=JobStatus.SCHEDULED)
                )
                payload = self._payload(job.id, status=JobStatus.PENDING)
                if await service.update(payload, condition=condition) is not None:
                    promoted += 1
        return promoted

    # ------------------------------------------------------------------
    # Claim + run
    # ------------------------------------------------------------------

    async def _claim_and_start(self, now: datetime) -> int:
        """Claim up to the free capacity of ``PENDING`` jobs and start each."""
        claimable: SearchFilter[Any] = AttrFilter(
            attribute="status", filter=EqFilter(value=JobStatus.PENDING)
        )
        claimed: list[UUID] = []
        attempts = 0
        max_attempts = self._max_concurrent * _CLAIM_ATTEMPT_FACTOR
        async with await self._resource.get_service() as service:
            while len(self._in_flight) + len(claimed) < self._max_concurrent:
                if attempts >= max_attempts:
                    break
                attempts += 1
                page = await service.search(claimable, limit=1)
                if not page.items:
                    break
                job = page.items[0]
                payload = self._payload(
                    job.id,
                    status=JobStatus.RUNNING,
                    claimed_by=self.runner_id,
                    claimed_at=now,
                    attempts=job.attempts + 1,
                )
                # The condition makes the check-and-write atomic: ``None`` means
                # another runner won the claim, so this one simply tries the next.
                if await service.update(payload, condition=claimable) is not None:
                    claimed.append(job.id)
        for job_id in claimed:
            self._start(job_id)
        return len(claimed)

    def _start(self, job_id: UUID) -> None:
        """Launch one job body concurrently, tracked so exit can drain it."""
        task = asyncio.create_task(self._run_job(job_id))
        self._in_flight.add(task)
        self._running_jobs[task] = job_id
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._in_flight.discard(task)
        self._running_jobs.pop(task, None)

    async def _run_job(self, job_id: UUID) -> None:
        """Load, run, and finish one job — with no session held while it runs."""
        try:
            job = await self._load(job_id)
            if job is None:
                return
            run = await self._invoke(job.job_details_kind, job.job_details)
            await self._finish(job, run)
        except asyncio.CancelledError:
            # Shutdown: leave the row RUNNING; ``__aexit__`` releases the claim.
            raise
        except Exception:
            logger.exception("Job %s failed unexpectedly.", job_id)

    async def _invoke(self, kind: str, details: Any) -> JobRun:
        """Run the stored body — a ``JobDetails`` instance, or a legacy mapping.

        The ``job_details`` column round-trips the model, so ``details`` is
        normally already the concrete kind. A raw mapping (an older row, or a
        backend that stored plain JSON) is validated here. A body that raises is
        caught and mapped to a terminal ``ERROR`` (rather than crashing the
        runner task); an unresolvable kind (a class the process has not imported)
        is likewise an ``ERROR``, never a silent no-op.
        """
        try:
            body = (
                details
                if isinstance(details, JobDetails)
                else JobDetails.model_validate({**details, "kind": kind})
            )
        except Exception as exc:
            logger.exception("Job kind %r could not be resolved.", kind)
            return JobRun(status="ERROR", detail=f"unresolvable job kind {kind!r}: {exc}")
        try:
            return await body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Job body %r raised.", kind)
            return JobRun(status="ERROR", detail=str(exc))

    async def _finish(self, job: Any, run: JobRun) -> bool:
        """Persist a run's outcome, conditional on this runner still owning the claim.

        The condition requires ``status == RUNNING`` **and** ``claimed_by == this
        runner``, so a runner whose claim was recovered (and possibly re-claimed)
        abandons its write and never overwrites a terminal state.

        A *failed* run requeues to ``PENDING`` (clearing the claim) while the
        attempt cap is not yet reached, and goes terminal ``ERROR`` at the cap;
        a successful run is terminal ``COMPLETED``. The retry decision is made
        here, against the freshly loaded row, because the attempt counter is
        only known once the body has finished. Returns whether the write applied.
        """
        if run.status == "ERROR" and job.attempts < job.max_attempts:
            payload = self._payload(
                job.id,
                status=JobStatus.PENDING,
                claimed_by=None,
                claimed_at=None,
                detail=run.detail,
            )
        else:
            payload = self._payload(job.id, status=run.status, detail=run.detail)
        condition = self._claim_condition(self.runner_id)
        async with await self._resource.get_service() as service:
            return await service.update(payload, condition=condition) is not None

    async def _release(self, job_id: UUID) -> None:
        """Return a still-owned ``RUNNING`` job to ``PENDING`` (best effort)."""
        condition = self._claim_condition(self.runner_id)
        payload = self._payload(job_id, status=JobStatus.PENDING, claimed_by=None, claimed_at=None)
        async with await self._resource.get_service() as service:
            await service.update(payload, condition=condition)

    # ------------------------------------------------------------------
    # Enqueue (the producer surface; ``tasks`` and app code use this)
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        details: JobDetails,
        *,
        status: JobStatus | str = JobStatus.PENDING,
        run_at: datetime | None = None,
        max_attempts: int | None = None,
        max_seconds_for_run: int | None = None,
        creator_id: UUID | None = None,
    ) -> Any:
        """Create a ``jobs`` row for ``details`` and return it.

        This is the producer surface: a scheduled task, an app request handler,
        or ordinary app code enqueues a durable job here (the REST ``POST /jobs``
        is the same create through the view). ``run_at`` schedules a one-shot;
        ``max_attempts`` / ``max_seconds_for_run`` default to the runner's config.
        """
        payload = self._payload(
            None,
            job_details_kind=type(details).__name__,
            job_details=details,
            status=JobStatus(status),
            run_at=run_at,
            max_attempts=(max_attempts if max_attempts is not None else self._default_max_attempts),
            max_seconds_for_run=max_seconds_for_run,
            creator_id=creator_id,
        )
        async with await self._resource.get_service() as service:
            return await service.create(payload)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dto_type(self) -> Any:
        return self._resource.get_dto_type()

    def _payload(self, job_id: UUID | None, **values: Any) -> Any:
        """Build an update / create DTO payload (``id`` included only when given)."""
        data = {k: v for k, v in values.items() if v is not None or k in _NULLABLE_FIELDS}
        if job_id is not None:
            data["id"] = job_id
        return self._dto_type().model_validate(data)

    def _claim_condition(self, claimed_by: UUID | None) -> SearchFilter[Any]:
        """``status == RUNNING AND claimed_by == <runner>`` (the ownership gate)."""
        return and_(
            AttrFilter(attribute="status", filter=EqFilter(value=JobStatus.RUNNING)),
            AttrFilter(attribute="claimed_by", filter=EqFilter(value=claimed_by)),
        )

    async def _load(self, job_id: UUID) -> Any:
        async with await self._resource.get_service() as service:
            try:
                return await service.read(job_id)
            except Exception:
                return None

    async def _enumerate(
        self, service: Service[Any, Any], condition: SearchFilter[Any]
    ) -> list[Any]:
        """Every row matching ``condition`` (paged), as a materialized list."""
        items: list[Any] = []
        cursor: str | None = None
        while True:
            page = await service.search(condition, cursor=cursor, limit=_ENUMERATE_PAGE)
            items.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return items


# Fields whose ``None`` is a meaningful value (not "omit this field"), so a
# payload may carry an explicit ``null`` for them — e.g. clearing ``claimed_by``
# on a requeue.
_NULLABLE_FIELDS = frozenset({"claimed_by", "claimed_at", "run_at", "detail", "creator_id"})


def runner_from_manifest(manifest: Manifest) -> JobRunner:
    """The :class:`JobRunner` among ``manifest``'s managers.

    Raises :class:`ValueError` when the manifest has no runner, naming the fix
    (register one in ``managers=``).
    """
    for manager in manifest.managers:
        if isinstance(manager, JobRunner):
            return manager
    raise ValueError(
        "No JobRunner found among the manifest's managers; register one with "
        "Manifest(resources=..., managers=[..., runner])."
    )
