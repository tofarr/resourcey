---
name: background-work
description: Long-running work — the cron task scheduler (BackgroundTask / BackgroundTaskScheduler / the tasks CLI) and durable, claimable jobs (JobDetails / Job / JobRunner). Load when adding scheduled tasks, background jobs, retries, or crash recovery.
version: "2.0.0"
---

# Background work

Two rungs. `tasks` runs in-process cron work; `jobs` makes a unit of work a
governed row so it survives a restart. Both are ordinary `Manifest` **managers**
— `core` stays unaware of either.

## `tasks` — the in-process scheduler

### `BackgroundTask`

A named, async callable whose config is injected through its constructor:

```python
class BackgroundTaskConfig(DiscriminatedUnionMixin, ABC):
    name: str
    schedule: str | None  # a 5-field cron expression, or None
    enabled: bool = True


class BackgroundTask(DiscriminatedUnionMixin, ABC):
    config: BackgroundTaskConfig

    async def __call__(self) -> None: ...
```

Both bases are `DiscriminatedUnionMixin`s keyed by `kind` (the class name), so a
deployment selects a concrete config by name without importing every subclass.

`BackgroundTasksConfig` (`APP_BACKGROUND_TASKS_*`) holds
`background_tasks_timezone`, `background_tasks_scheduler_enabled`, and the
`background_tasks` list (`APP_BACKGROUND_TASKS_<n>_KIND` / `_NAME` /
`_SCHEDULE` / `_ENABLED` plus kind fields). `for_task(name, kind)` returns the
matching entry — which must already be the expected `kind`, so a mis-paired
entry fails loudly — or a defaults-only `kind(name=name)`, so a task works
unconfigured. `get_task(name)` rejects a duplicate name; `assert_unique_names`
is the loud-at-startup check the scheduler runs.

### `cron.py`

An **internal** 5-field parser, no new dependency: `*`, `*/n`, fixed, ranges,
steps over ranges, comma lists, month / weekday names, and the `@`-shorthands
(`@hourly` / `@daily` / `@weekly` / `@monthly` / `@yearly`).
`CronSchedule.next_fire(after)` rolls forward a whole unit at a time to the
first matching minute; `matches(moment)` is the field conjunction with the
**DOM/DOW-both-restricted OR** rule. A seconds field, `@reboot`, and quartz
extensions (`?` / `L` / `W`) are out of scope; a malformed expression raises
`CronError`.

### `BackgroundTaskScheduler`

```python
BackgroundTaskScheduler(tasks, *, config=None, timezone=None)
```

* Its constructor rejects duplicate names and parses every schedule, so a typo
  fails at startup rather than silently never firing.
* `__aenter__` starts **one** asyncio loop (unless
  `background_tasks_scheduler_enabled` is false); `__aexit__` cancels the loop
  and any in-flight runs.
* The loop sleeps to the earliest next fire and calls `tick(now)` — a public,
  deterministic seam that starts each active scheduled task via `_start`,
  **concurrently** and tracked.
* A raising task is caught / logged and the loop continues. There are **no
  retries and no mutual exclusion** — a schedule shorter than a run simply
  overlaps, so a task that must not overlap enforces that itself.
* `run_once(name=None)` runs one task by name (even a disabled one) or every
  enabled task, once, without starting the loop.
* `scheduler_from_manifest(manifest)` finds the scheduler among the managers.

### CLI

```bash
python -m resourcey tasks list app:manifest
python -m resourcey tasks run  app:manifest [name]
```

`list` prints each task's schedule / enabled flag. `run` enters the manifest's
managers **except** the scheduler, so a storage-backed task finds a live engine
while the periodic loop stays dormant — the external-cron / `kubectl` path. The
framework does no `.env` loading, so run it with the app's environment populated
(`uv run --env-file .env python -m resourcey ...`).

See `specs/background_tasks.qnt`.

## `jobs` — durable, claimable work

Unlike the in-process scheduler, a unit of work here is a **governed row**, so it
is durable, claimed exactly once, retried, and recovered after a crash.

### `JobDetails`

```python
class JobDetails(DiscriminatedUnionMixin, ABC):
    async def __call__(self) -> JobRun: ...


class JobRun(BaseModel):
    status: str  # COMPLETED / ERROR
    detail: str | None
```

A stored job is **data that names its own behavior**: the row keeps the
discriminator (`job_details_kind`, the class name) and the details, and the
runner deserializes them back before invoking — no app import list, since the
mixin routes on `kind`. The framework ships one trivial reference kind,
`LogJobDetails`. A body that raises is mapped to `ERROR` by the runner.

### The `jobs` row

`Job` (on `JobsBase`) has `id` (uuid4), `job_details_kind`, `job_details` (a
`ModelType(JobDetails)` column — typed as the model, stored as JSON, so a row
round-trips the concrete kind and the generated request models carry the
discriminated-union schema), `status`, `detail`, `run_at`, `claimed_by` /
`claimed_at`, `attempts` / `max_attempts` / `max_seconds_for_run`, `creator_id`,
and the framework timestamps.

`JobStatus` = `PENDING` / `SCHEDULED` / `RUNNING` / `COMPLETED` / `ERROR` /
`CANCELLED`. `PENDING` and `SCHEDULED` are claimable; `RUNNING` is in flight;
`COMPLETED` / `ERROR` / `CANCELLED` are **terminal** and never transition.

The runner-managed fields (`job_details_kind`, `claimed_by`, `claimed_at`,
`attempts`, `creator_id`) carry `RUNNER_FIELD`
(`in_create_request=False, in_update_request=False`), so a client cannot write
them.

Two resources:

* `jobs_resource(...)` — the **inner** resource the runner writes through.
* `jobs_view(inner)` — the public `ResourceView` that hides the runner-managed
  fields and narrows `status`'s *request* type to `CLIENT_STATUS_TYPE`
  (`PENDING` / `SCHEDULED` / `CANCELLED`) while the read model keeps the full
  `JobStatus`, so a job the runner took to `RUNNING` / `COMPLETED` / `ERROR`
  still reads back.

`JobsService` enforces two rules: `create` derives `job_details_kind` from the
body's `kind` and stamps `creator_id` from the authenticated principal
(`PRINCIPAL_CTX_KEY` — the `Owner` policy leaves `create` unscoped, so a new row
has no owner until here); `update` folds a **not-terminal** condition into every
client write, so a terminal job never transitions — an update of one is a no-op
returning `None`, never an error.

### `JobRunner`

```python
JobRunner(resource, *, config=None, runner_id=None, sweep_interval=None,
          max_concurrent=None, default_max_seconds_for_run=None,
          default_max_attempts=None, enabled=None)
```

An ordinary `Manifest` manager. On construction it generates a **process-unique**
`runner_id` (a fresh `uuid4`, never reused across restarts, so a stale claim is
never mistaken for a live one). On a sweep interval it:

1. **Recovers** stale claims and promotes due `SCHEDULED` jobs;
2. **Claims** up to its free capacity of `PENDING` jobs;
3. **Runs** each as a background task with **no database session held open**;
4. **Completes** each run conditionally.

Coordination is the conditional write: both the claim (`status == PENDING`) and
the completion (`status == RUNNING AND claimed_by == this runner`) are
`Service.update(payload, condition=...)` calls whose `None` result is the
verdict — check and write are one atomic operation, so of any number of racing
runners at most one wins and a loser cannot distinguish a claim miss from an
absent row. No `SELECT … FOR UPDATE SKIP LOCKED`.

A *failed* run requeues to `PENDING` while `attempts < max_attempts`, else goes
terminal `ERROR`; a success is terminal `COMPLETED`. A stale `RUNNING` claim
older than the job's own `max_seconds_for_run` (falling back to the config
default, fail-closed) is recovered to `PENDING` under the cap or terminal
`ERROR` at it, so no job is ever stuck. Entering is gated by
`jobs_runner_enabled`, so a web replica runs the manifest without claiming jobs;
a graceful exit releases each in-flight job's claim back to `PENDING`.

`enqueue(details, *, status=PENDING, run_at=None, max_attempts=None,
max_seconds_for_run=None, creator_id=None)` is the **producer** surface (the
REST `POST /jobs` is the same create through the view). `sweep(now=None)` is
public and deterministic so a test drives it with a chosen `now` instead of
waiting on the loop. `runner_from_manifest(manifest)` finds the runner among the
managers.

### `JobsConfig`

`APP_JOBS_SWEEP_INTERVAL_SECONDS`, `APP_JOBS_DEFAULT_MAX_SECONDS_FOR_RUN`,
`APP_JOBS_MAX_CONCURRENT_JOBS`, `APP_JOBS_DEFAULT_MAX_ATTEMPTS`,
`APP_JOBS_RUNNER_ENABLED`. Fields are prefixed so they do not collide in the
shared flat config namespace.

See `specs/jobs.qnt` and `examples/10_jobs`.
