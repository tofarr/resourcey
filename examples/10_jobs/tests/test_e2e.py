"""End-to-end REST + live-runner tests for the durable-jobs example (SQLite).

Each test runs against an **isolated SQLite database file** created by the
committed Alembic migration, and drives the full request → auth → role → service
→ SQLAlchemy stack over httpx's ASGI transport. A real
:class:`~resourcey.jobs.jobs_runner.JobRunner` is entered with the manifest, so a
job enqueued through REST is genuinely claimed and run.

Covered: the runner-managed fields are hidden from the wire; a USER's jobs are
scoped to it while an ADMIN sees all; a client may cancel a ``PENDING`` job but
not a terminal one; a job runs to ``COMPLETED`` with its ``detail``; a failing
job retries up to ``max_attempts`` and then goes terminal ``ERROR``; a scheduled
job is promoted when due; and the enqueue path stamps ``creator_id``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import AsyncClient

from resourcey.auth.auth_api_key import API_KEY_HEADER_NAME
from resourcey.jobs.jobs_model import Job, JobStatus
from resourcey.util.search_filter import AllFilter, AttrFilter, EqFilter, and_

ADMIN_KEY = "admin-key"
USER_KEY = "user-key"
USER_UUID = UUID("11111111-1111-1111-1111-111111111111")


def _h(key: str) -> dict[str, str]:
    return {API_KEY_HEADER_NAME: key}


async def _enqueue(
    client: AsyncClient,
    key: str,
    kind: str,
    *,
    status: str | None = None,
    max_attempts: int | None = None,
    **details: object,
) -> dict:
    body: dict[str, object] = {"job_details": {"kind": kind, **details}}
    if status is not None:
        body["status"] = status
    if max_attempts is not None:
        body["max_attempts"] = max_attempts
    resp = await client.post("/jobs", json=body, headers=_h(key))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _find(manager, **eq: object) -> list[Job]:
    """Read rows straight off the store (the runner's own view of the table)."""
    condition = AllFilter()
    for attribute, value in eq.items():
        condition = and_(condition, AttrFilter(attribute=attribute, filter=EqFilter(value=value)))
    maker = await manager.get_session_maker()
    from resourcey.jobs.jobs_model import jobs_resource

    resource = jobs_resource(session_factory=maker)
    async with await resource.get_service() as service:
        page = await service.search(condition, limit=100)
        return list(page.items)


async def _wait_until(manager, predicate, *, timeout: float = 5.0):
    """Poll the store until ``predicate`` holds (the live runner is asynchronous)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        jobs = await _find(manager, job_details_kind="EchoJobDetails")
        jobs += await _find(manager, job_details_kind="BoomJobDetails")
        if predicate(jobs):
            return jobs
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"timed out waiting; jobs={jobs}")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# The wire shape: runner-managed fields are hidden
# ---------------------------------------------------------------------------


async def test_create_response_hides_runner_managed_fields(client: AsyncClient) -> None:
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="hi")
    assert {"claimed_by", "claimed_at", "attempts"}.isdisjoint(created)
    assert created["status"] == "PENDING"


async def test_client_cannot_write_a_runner_status(client: AsyncClient) -> None:
    resp = await client.post(
        "/jobs",
        json={"job_details": {"kind": "EchoJobDetails", "message": "x"}, "status": "RUNNING"},
        headers=_h(USER_KEY),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# The live runner: enqueue -> claim -> run -> COMPLETED
# ---------------------------------------------------------------------------


async def test_enqueued_job_runs_to_completion(client: AsyncClient, wired: tuple) -> None:
    _client, manager, _app, _runner = wired
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="run me")
    jobs = await _wait_until(
        manager,
        lambda js: any(j.id == UUID(created["id"]) and j.status == JobStatus.COMPLETED for j in js),
    )
    job = next(j for j in jobs if j.id == UUID(created["id"]))
    assert job.status == JobStatus.COMPLETED
    assert job.detail == "echoed: run me"
    assert job.attempts == 1
    assert job.claimed_by is not None  # a real claim happened


async def test_enqueue_stamps_the_creator_from_the_principal(
    client: AsyncClient, wired: tuple
) -> None:
    _client, manager, _app, _runner = wired
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="owned")
    jobs = await _find(manager, job_details_kind="EchoJobDetails")
    job = next(j for j in jobs if j.id == UUID(created["id"]))
    assert job.creator_id == USER_UUID


# ---------------------------------------------------------------------------
# Retries: a failing job requeues then goes terminal ERROR at the cap
# ---------------------------------------------------------------------------


async def test_failing_job_retries_then_errors(client: AsyncClient, wired: tuple) -> None:
    _client, manager, _app, _runner = wired
    created = await _enqueue(client, USER_KEY, "BoomJobDetails", max_attempts=3)
    job_id = UUID(created["id"])
    jobs = await _wait_until(
        manager,
        lambda js: any(j.id == job_id and j.status == JobStatus.ERROR for j in js),
        timeout=10.0,
    )
    job = next(j for j in jobs if j.id == job_id)
    assert job.status == JobStatus.ERROR
    assert job.attempts == 3  # one per claim, capped
    assert job.detail == "boom"


# ---------------------------------------------------------------------------
# Scheduling: a due SCHEDULED job is promoted and run
# ---------------------------------------------------------------------------


async def test_scheduled_job_is_promoted_when_due(client: AsyncClient, wired: tuple) -> None:
    _client, manager, _app, _runner = wired
    created = await _enqueue(
        client, USER_KEY, "EchoJobDetails", status="SCHEDULED", message="later"
    )
    job_id = UUID(created["id"])
    jobs = await _wait_until(
        manager,
        lambda js: any(j.id == job_id and j.status == JobStatus.COMPLETED for j in js),
    )
    assert next(j for j in jobs if j.id == job_id).status == JobStatus.COMPLETED


async def test_future_scheduled_job_is_not_run_yet(client: AsyncClient, wired: tuple) -> None:
    _client, manager, _app, _runner = wired
    future = datetime.now(UTC) + timedelta(hours=1)
    maker = await manager.get_session_maker()
    # Create via the inner resource so we can set a future run_at (the public
    # create request deliberately keeps run_at client-settable; this just pins a
    # far-future value directly).
    from resourcey.jobs.jobs_model import jobs_resource

    resource = jobs_resource(session_factory=maker)
    async with await resource.get_service() as service:
        dto = resource.get_dto_type()
        created = await service.create(
            dto.model_validate(
                {
                    "job_details": {"kind": "EchoJobDetails", "message": "future"},
                    "status": "SCHEDULED",
                    "run_at": future,
                }
            )
        )
    await asyncio.sleep(0.3)
    jobs = await _find(manager, job_details_kind="EchoJobDetails")
    job = next(j for j in jobs if j.id == created.id)
    assert job.status == JobStatus.SCHEDULED  # still not due


# ---------------------------------------------------------------------------
# Cancellation + terminal stickiness
# ---------------------------------------------------------------------------


async def test_user_cancels_its_own_pending_job(client: AsyncClient, wired: tuple) -> None:
    _client, _manager, _app, runner = wired
    # A paused runner so the job stays PENDING while we cancel it.
    runner._enabled = False
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="cancel me")
    resp = await client.patch(
        f"/jobs/{created['id']}", json={"status": "CANCELLED"}, headers=_h(USER_KEY)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CANCELLED"


async def test_terminal_job_cannot_be_revived(client: AsyncClient, wired: tuple) -> None:
    _client, _manager, _app, runner = wired
    runner._enabled = False
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="done")
    # Cancel it (PENDING -> CANCELLED is allowed), then a revive attempt misses.
    assert (
        await client.patch(
            f"/jobs/{created['id']}", json={"status": "CANCELLED"}, headers=_h(USER_KEY)
        )
    ).status_code == 200
    revived = await client.patch(
        f"/jobs/{created['id']}", json={"status": "PENDING"}, headers=_h(USER_KEY)
    )
    # A terminal job never transitions; the miss is a 404 (indistinguishable from
    # an absent row — the framework's no-existence-leak convention).
    assert revived.status_code == 404, revived.text


# ---------------------------------------------------------------------------
# RBAC scoping
# ---------------------------------------------------------------------------


async def test_admin_sees_all_jobs_but_a_user_sees_only_its_own(
    client: AsyncClient, wired: tuple
) -> None:
    _client, manager, _app, runner = wired
    runner._enabled = False
    # A job created with no principal (creator_id NULL) is unreachable by the
    # USER's Owner scope; the ADMIN sees it.
    maker = await manager.get_session_maker()
    from resourcey.jobs.jobs_model import jobs_resource

    resource = jobs_resource(session_factory=maker)
    async with await resource.get_service() as service:
        dto = resource.get_dto_type()
        await service.create(
            dto.model_validate({"job_details": {"kind": "EchoJobDetails", "message": "anon"}})
        )
    mine = await _enqueue(client, USER_KEY, "EchoJobDetails", message="mine")

    user_page = await client.get("/jobs", headers=_h(USER_KEY))
    user_ids = {row["id"] for row in user_page.json()["items"]}
    assert mine["id"] in user_ids
    assert len(user_ids) == 1  # only the USER's own job

    admin_page = await client.get("/jobs", headers=_h(ADMIN_KEY))
    admin_ids = {row["id"] for row in admin_page.json()["items"]}
    assert {mine["id"]} <= admin_ids
    assert len(admin_ids) == 2  # the NULL-creator job and the USER's


async def test_user_cannot_cancel_another_principals_job(client: AsyncClient, wired: tuple) -> None:
    _client, manager, _app, runner = wired
    runner._enabled = False
    maker = await manager.get_session_maker()
    from resourcey.jobs.jobs_model import jobs_resource

    resource = jobs_resource(session_factory=maker)
    async with await resource.get_service() as service:
        dto = resource.get_dto_type()
        theirs = await service.create(
            dto.model_validate({"job_details": {"kind": "EchoJobDetails", "message": "theirs"}})
        )
    resp = await client.patch(
        f"/jobs/{theirs.id}", json={"status": "CANCELLED"}, headers=_h(USER_KEY)
    )
    assert resp.status_code == 404, resp.text
    # The row is untouched.
    jobs = await _find(manager, job_details_kind="EchoJobDetails")
    assert next(j for j in jobs if j.id == theirs.id).status == JobStatus.PENDING


async def test_anonymous_caller_sees_nothing(client: AsyncClient, wired: tuple) -> None:
    runner = wired[3]
    runner._enabled = False
    created = await _enqueue(client, USER_KEY, "EchoJobDetails", message="private")
    # A denied search yields an empty page, never an error (the framework's
    # documented search semantics), so an anonymous caller enumerates nothing.
    anon = await client.get("/jobs")
    assert anon.status_code == 200, anon.text
    assert anon.json()["items"] == []
    # And a by-id read of a real job is a 404 (existence not leaked).
    by_id = await client.get(f"/jobs/{created['id']}")
    assert by_id.status_code == 404, by_id.text


# ---------------------------------------------------------------------------
# The runner's own producer surface
# ---------------------------------------------------------------------------


async def test_runner_enqueue_creates_a_claimable_job(wired: tuple) -> None:
    _client, manager, _app, runner = wired
    from jobs_example.jobs import EchoJobDetails

    created = await runner.enqueue(EchoJobDetails(message="from the runner"))
    jobs = await _wait_until(
        manager, lambda js: any(j.id == created.id and j.status == JobStatus.COMPLETED for j in js)
    )
    assert next(j for j in jobs if j.id == created.id).detail == "echoed: from the runner"
