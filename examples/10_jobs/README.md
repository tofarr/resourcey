# Example 10 — durable, claimable background jobs

A message-board-free example that shows the framework's **durable job** rung
(issue #16) on top of the in-process task scheduler (issue #15). A request (or a
scheduled task) **enqueues** a job; a pool of workers **claims** and runs it
**exactly once**, with retries and crash recovery.

## Why a job framework on top of `resourcey.tasks`?

`resourcey.tasks` runs in-process cron work. It has **no durability** (a crash
loses the tick), **no run history**, **no mutual exclusion** (two replicas can
run the same task), and **no retries**. `resourcey.jobs` adds exactly those, by
making each unit of work a **governed row**:

* **Durable** — a job is a `jobs` table row; it survives a restart.
* **Exactly-once** — a claim is one **conditional write**
  (`update(..., condition=status == PENDING)`), so of any number of racing
  workers at most one wins; a loser sees `None` (indistinguishable from an
  absent row) and claims nothing.
* **Crash recovery** — `runner_id` is fresh per process and never reused, and a
  `RUNNING` claim older than its bound is swept back to `PENDING` (or terminal
  `ERROR` at the attempt cap), so no job is ever stuck.
* **Retries** — a failed run requeues while `attempts < max_attempts`, else goes
  terminal `ERROR`.

## The pieces

| Concern | Where |
| --- | --- |
| The job body (polymorphic, stored as JSON) | `jobs_example/jobs.py` — `EchoJobDetails`, `SlowJobDetails`, `BoomJobDetails` |
| The `jobs` table + governed resource | `resourcey.jobs.jobs_model` — `Job`, `jobs_resource`, `jobs_view` |
| The claim / run / recover loop | `resourcey.jobs.jobs_runner.JobRunner` (a `Manifest` manager) |
| The scheduled producer | `jobs_example/tasks.py` — `EnqueueEchoTask` (a `resourcey.tasks` task) |
| Auth / RBAC | `jobs_example/app.py` — the example-04 role wiring |

The app exposes the `jobs` resource over the ordinary REST surface, with the
runner-managed fields (`claimed_by`, `claimed_at`, `attempts`) hidden and
`status` narrowed to the client-writable set (`PENDING` / `SCHEDULED` /
`CANCELLED`). A `USER` may enqueue, read, and cancel **its own** jobs (the
`Owner` policy scopes on `creator_id`); an `ADMIN` sees everything.

## Run it

```bash
uv sync --extra test
uv run --env-file .env alembic upgrade head     # create the jobs table
uv run --env-file .env uvicorn jobs_example.app:app --port 8090
```

Enqueue a job (a `USER` key enqueues; the runner claims it within a couple of
seconds):

```bash
curl -s localhost:8090/jobs -H 'X-API-Key: user-key' \
  -H 'content-type: application/json' \
  -d '{"job_details": {"kind": "EchoJobDetails", "message": "hello"}}'
```

Read it back — the `status` moves `PENDING` → `COMPLETED` and the `detail`
carries the echoed value:

```bash
curl -s localhost:8090/jobs -H 'X-API-Key: user-key'
```

A failing job with retries (`max_attempts: 3`) requeues twice, then goes
terminal `ERROR`:

```bash
curl -s localhost:8090/jobs -H 'X-API-Key: user-key' \
  -H 'content-type: application/json' \
  -d '{"job_details": {"kind": "BoomJobDetails"}, "max_attempts": 3}'
```

The scheduled `EnqueueEchoTask` (`*/30 * * * *`) also enqueues a durable job each
tick — the `resourcey.tasks` → `resourcey.jobs` bridge.

## Running workers separately

`APP_JOBS_RUNNER_ENABLED` gates whether *this* process claims jobs. Run the API
replicas with it `false` and a dedicated worker process with it `true`, so the
web tier never competes for work:

```bash
# API replica
APP_JOBS_RUNNER_ENABLED=false uv run --env-file .env uvicorn jobs_example.app:app --port 8090
# Worker (same app, runner on, scheduler off)
APP_JOBS_RUNNER_ENABLED=true uv run --env-file .env uvicorn jobs_example.app:app --port 8091
```

## Tests

```bash
uv run --extra test pytest
```

The suite builds the app through the real config path, applies the committed
migration, and drives the full request → auth → role → service → SQLAlchemy
stack over httpx's ASGI transport — including a live runner that claims and runs
enqueued jobs end to end.
