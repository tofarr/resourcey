"""Tests for the durable job framework (issue #16).

Exercised against the **real** production code paths (no mocks): a real
:class:`~resourcey.jobs.jobs_model.Job` ``SqlResource`` over in-memory SQLite, a
real :class:`~resourcey.jobs.jobs_runner.JobRunner` entered through a real
:class:`~resourcey.core.manifest.Manifest`, and the real conditional-write
primitive (#164). Time is controlled by driving the runner's deterministic
:meth:`~resourcey.jobs.jobs_runner.JobRunner.sweep` with a chosen ``now`` rather
than by faking a clock.

Covered: the ``JobDetails`` round-trip, the restricted client status set, the
runner-managed fields being hidden / not writable, terminal stickiness, the
process-unique ``runner_id``, the ``jobs_runner_enabled`` gate, single-winner
claim under concurrency, completion abandonment after a recover, stale-claim
recovery (requeue vs. terminal ``ERROR``), the attempt cap, the
no-session-held-while-running rule, ``run_at`` scheduling, and RBAC scoping over
REST.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import PrivateAttr, SecretStr, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.auth.auth_policy import AllowAll, Owner
from resourcey.auth.auth_role import AppRole, RolePolicyResolver
from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.jobs.jobs_config import DEFAULT_MAX_SECONDS_FOR_RUN, JobsConfig
from resourcey.jobs.jobs_details import JobDetails, JobRun, LogJobDetails
from resourcey.jobs.jobs_model import (
    CLIENT_STATUS_TYPE,
    Job,
    JobsBase,
    JobStatus,
    jobs_resource,
    jobs_view,
)
from resourcey.jobs.jobs_runner import JobRunner, runner_from_manifest
from resourcey.sql.sql_service import SqlService
from resourcey.util.search_filter import AllFilter, AttrFilter, EqFilter

# ---------------------------------------------------------------------------
# Test doubles (real subclasses of the real base — no mocking)
# ---------------------------------------------------------------------------


class BoomJob(JobDetails):
    """A job body that always raises, to exercise the failure / retry path."""

    async def __call__(self) -> JobRun:
        raise RuntimeError("boom")


class ProbeJob(JobDetails):
    """A job body that records whether any DB service was open while it ran.

    The runner must not hold a session open across the body, so the probe reads
    the shared :data:`_OPEN_SERVICES` counter at invocation time and stores it.
    """

    _observed_open_services: int = PrivateAttr(default=-1)

    async def __call__(self) -> JobRun:
        self._observed_open_services = _OPEN_SERVICES
        _PROBE_RESULTS.append(_OPEN_SERVICES)
        return JobRun(status="COMPLETED")


_OPEN_SERVICES = 0
_PROBE_RESULTS: list[int] = []


class InstrumentedJobsService(SqlService[object, object]):
    """A ``jobs`` service that tracks how many services are concurrently open."""

    async def __aenter__(self) -> InstrumentedJobsService:
        global _OPEN_SERVICES
        await super().__aenter__()
        _OPEN_SERVICES += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        global _OPEN_SERVICES
        _OPEN_SERVICES -= 1
        await super().__aexit__(*exc)


class InstrumentedJobsResource(type(jobs_resource())):  # type: ignore[misc]
    """The real ``jobs`` resource with an instrumented service."""

    def make_service(self, ctx, session_factory):  # type: ignore[no-untyped-def]
        return InstrumentedJobsService(self, ctx, session_factory)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def jobs() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], object]]:
    """An in-memory SQLite store and the **inner** ``jobs`` resource over it."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(JobsBase.metadata.create_all)
    yield maker, jobs_resource(session_factory=maker)
    await engine.dispose()


def _config(**overrides: object) -> JobsConfig:
    """A config with a long sweep interval (tests drive ``sweep`` directly)."""
    base: dict[str, object] = {"jobs_sweep_interval_seconds": 999, "jobs_max_concurrent_jobs": 4}
    base.update(overrides)
    return JobsConfig(**base)  # type: ignore[arg-type]


async def _read(inner: object, job_id: UUID) -> object:
    async with await inner.get_service() as service:  # type: ignore[attr-defined]
        return await service.read(job_id)


async def _find(inner: object, **eq: object) -> list[object]:
    condition = AllFilter()
    for attribute, value in eq.items():
        condition = AttrFilter(attribute=attribute, filter=EqFilter(value=value))
    async with await inner.get_service() as service:  # type: ignore[attr-defined]
        page = await service.search(condition, limit=100)
        return list(page.items)


# ---------------------------------------------------------------------------
# JobDetails — the polymorphic body
# ---------------------------------------------------------------------------


def test_job_details_round_trips_to_its_concrete_subclass():
    body = JobDetails.model_validate({"kind": "LogJobDetails", "message": "hi"})
    assert isinstance(body, LogJobDetails)
    assert body.message == "hi"


def test_unknown_job_kind_is_rejected():
    with pytest.raises(ValueError, match="Unknown kind"):
        JobDetails.model_validate({"kind": "NoSuchKind"})


async def test_log_job_details_reports_completion():
    run = await LogJobDetails(message="hello")()
    assert run.status == "COMPLETED"
    assert run.detail == "hello"


# ---------------------------------------------------------------------------
# The row + its public view
# ---------------------------------------------------------------------------


def test_job_is_model_first_with_the_expected_fields(jobs):
    _maker, inner = jobs
    fields = set(inner.get_dto_type().model_fields)
    assert {
        "id",
        "job_details_kind",
        "job_details",
        "status",
        "detail",
        "run_at",
        "claimed_by",
        "claimed_at",
        "attempts",
        "max_attempts",
        "max_seconds_for_run",
        "creator_id",
        "created_at",
        "updated_at",
    } <= fields
    assert inner.get_resource_path() == "jobs"


def test_public_view_hides_runner_managed_fields(jobs):
    _maker, inner = jobs
    view = jobs_view(inner)
    read_fields = set(view.get_rest_models().read_response.model_fields)
    assert {"claimed_by", "claimed_at", "attempts"}.isdisjoint(read_fields)
    assert {"status", "job_details", "max_attempts"} <= read_fields


def test_public_view_narrows_the_writable_status_set(jobs):
    _maker, inner = jobs
    view = jobs_view(inner)
    request_type = view.get_rest_models().create_request.model_fields["status"].annotation
    assert request_type == CLIENT_STATUS_TYPE
    # A runner-managed status is not accepted on the wire...
    with pytest.raises(ValidationError):
        view.get_rest_models().create_request.model_validate(
            {"job_details": {"kind": "LogJobDetails", "message": "x"}, "status": "RUNNING"}
        )
    # ...but a client-settable one is.
    ok = view.get_rest_models().create_request.model_validate(
        {"job_details": {"kind": "LogJobDetails", "message": "x"}, "status": "SCHEDULED"}
    )
    assert ok.status == "SCHEDULED"
    # The read model keeps the full enum, so a finished job still reads back.
    read_type = view.get_rest_models().read_response.model_fields["status"].annotation
    assert read_type is JobStatus


async def test_runner_managed_fields_are_not_client_writable(jobs):
    _maker, inner = jobs
    view = jobs_view(inner)
    create_fields = set(view.get_rest_models().create_request.model_fields)
    update_fields = set(view.get_rest_models().update_request.model_fields)
    assert {"claimed_by", "claimed_at", "attempts", "creator_id", "job_details_kind"}.isdisjoint(
        create_fields | update_fields
    )


async def test_terminal_status_is_sticky_over_the_public_surface(jobs):
    _maker, inner = jobs
    view = jobs_view(inner)
    runner = JobRunner(inner, config=_config(), enabled=False)
    created = await runner.enqueue(LogJobDetails(message="x"))
    async with await view.get_service() as service:
        dto = view.get_dto_type()
        cancelled = await service.update(
            dto.model_validate({"id": created.id, "status": "CANCELLED"})
        )
        assert cancelled is not None and cancelled.status == JobStatus.CANCELLED
        # A terminal job never transitions: the update is a no-op (None).
        revived = await service.update(dto.model_validate({"id": created.id, "status": "PENDING"}))
        assert revived is None
    assert (await _read(inner, created.id)).status == JobStatus.CANCELLED


# ---------------------------------------------------------------------------
# The runner: identity, lifecycle, gating
# ---------------------------------------------------------------------------


def test_runner_id_is_process_unique(jobs):
    _maker, inner = jobs
    a = JobRunner(inner, config=_config(), enabled=False)
    b = JobRunner(inner, config=_config(), enabled=False)
    assert a.runner_id != b.runner_id


async def test_runner_starts_and_stops_its_loop(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(jobs_sweep_interval_seconds=0.01))
    async with runner:
        assert runner.entered is True
    assert runner.entered is False


async def test_runner_is_gated_by_config(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(jobs_runner_enabled=False))
    await runner.enqueue(LogJobDetails(message="x"))
    async with runner:
        # The loop never started, so nothing was claimed.
        assert runner.entered is True
    assert await _find(inner, status=JobStatus.PENDING)


def test_runner_from_manifest_finds_it(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    manifest = Manifest(resources=[], managers=[runner])
    assert runner_from_manifest(manifest) is runner
    with pytest.raises(ValueError, match="No JobRunner"):
        runner_from_manifest(Manifest(resources=[], managers=[]))


# ---------------------------------------------------------------------------
# Claim under concurrency
# ---------------------------------------------------------------------------


async def test_two_runners_never_claim_the_same_job(jobs):
    _maker, inner = jobs
    config = _config()
    await JobRunner(inner, config=config, enabled=False).enqueue(LogJobDetails(message="one"))
    first = JobRunner(inner, config=config, enabled=False)
    second = JobRunner(inner, config=config, enabled=False)
    claimed_first = await first.sweep()
    claimed_second = await second.sweep()
    assert (claimed_first, claimed_second) == (1, 0)
    await _drain()
    job = (await _find(inner, job_details_kind="LogJobDetails"))[0]
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 1


# ---------------------------------------------------------------------------
# Completion abandonment after a recover
# ---------------------------------------------------------------------------


async def test_completion_abandons_a_recovered_claim(jobs):
    _maker, inner = jobs
    config = _config()
    runner_a = JobRunner(inner, config=config, enabled=False, runner_id=uuid4())
    await runner_a.enqueue(LogJobDetails(message="x"))
    # A claims the job (starts a run we then let finish), then we simulate a
    # recover: force the row back to PENDING with a backdated claim, and let B
    # claim it. A's completion must then be refused.
    job = (await _find(inner, status=JobStatus.PENDING))[0]
    condition = AttrFilter(attribute="status", filter=EqFilter(value=JobStatus.PENDING))
    claimed = await _conditional_claim(inner, job.id, runner_a.runner_id, condition)
    assert claimed is not None
    await _backdate_claim(inner, job.id, seconds=7200)
    runner_b = JobRunner(inner, config=config, enabled=False, runner_id=uuid4())
    recovered = await runner_b._recover_stale(datetime.now(UTC))
    assert recovered == 1
    await _drain()
    # A's late completion for the same id must not overwrite B's state.
    from resourcey.jobs.jobs_details import JobRun

    wrote = await runner_a._finish(await _read(inner, job.id), JobRun(status="COMPLETED"))
    # The row is no longer RUNNING/owned by A, so the conditional write misses.
    assert wrote is False


# ---------------------------------------------------------------------------
# Stale-claim recovery + the attempt cap
# ---------------------------------------------------------------------------


async def test_stale_claim_requeues_while_under_the_cap(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    job = await runner.enqueue(LogJobDetails(message="x"), max_attempts=2)
    await _force_running(inner, job.id, claimed_by=uuid4(), attempts=1, seconds_ago=7200)
    assert await runner._recover_stale(datetime.now(UTC)) == 1
    recovered = await _read(inner, job.id)
    assert recovered.status == JobStatus.PENDING
    assert recovered.claimed_by is None and recovered.claimed_at is None


async def test_stale_claim_goes_terminal_error_at_the_cap(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    job = await runner.enqueue(LogJobDetails(message="x"), max_attempts=2)
    await _force_running(inner, job.id, claimed_by=uuid4(), attempts=2, seconds_ago=7200)
    assert await runner._recover_stale(datetime.now(UTC)) == 1
    assert (await _read(inner, job.id)).status == JobStatus.ERROR


async def test_unset_max_seconds_uses_the_fail_closed_default(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    job = await runner.enqueue(LogJobDetails(message="x"), max_attempts=2)
    assert job.max_seconds_for_run is None
    # Younger than the 3600s default: not stale.
    await _force_running(inner, job.id, claimed_by=uuid4(), attempts=1, seconds_ago=10)
    assert await runner._recover_stale(datetime.now(UTC)) == 0
    # Older than the default: stale.
    await _force_running(
        inner, job.id, claimed_by=uuid4(), attempts=1, seconds_ago=DEFAULT_MAX_SECONDS_FOR_RUN + 10
    )
    assert await runner._recover_stale(datetime.now(UTC)) == 1


async def test_a_failing_job_requeues_then_errors_at_the_cap(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    job = await runner.enqueue(BoomJob(), max_attempts=2)
    await runner.sweep()
    await _drain()
    after_first = await _read(inner, job.id)
    assert after_first.status == JobStatus.PENDING and after_first.attempts == 1
    await runner.sweep()
    await _drain()
    after_second = await _read(inner, job.id)
    assert after_second.status == JobStatus.ERROR and after_second.attempts == 2


# ---------------------------------------------------------------------------
# run_at scheduling
# ---------------------------------------------------------------------------


async def test_a_future_scheduled_job_is_not_claimed_until_due(jobs):
    _maker, inner = jobs
    runner = JobRunner(inner, config=_config(), enabled=False)
    now = datetime.now(UTC)
    future = await runner.enqueue(
        LogJobDetails(message="later"), status=JobStatus.SCHEDULED, run_at=now + timedelta(hours=1)
    )
    assert await runner.sweep(now=now) == 0
    assert (await _read(inner, future.id)).status == JobStatus.SCHEDULED
    # Once due, the promotion makes it claimable.
    claimed = await runner.sweep(now=now + timedelta(hours=2))
    assert claimed == 1
    await _drain()
    assert (await _read(inner, future.id)).status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# No session held while the body runs
# ---------------------------------------------------------------------------


async def test_no_session_is_held_while_a_job_body_runs(jobs):
    _maker, _inner = jobs
    _PROBE_RESULTS.clear()
    instrumented = InstrumentedJobsResource(Job, session_factory=_maker)
    runner = JobRunner(instrumented, config=_config(), enabled=False)
    await runner.enqueue(ProbeJob())
    await runner.sweep()
    await _drain()
    assert _PROBE_RESULTS == [0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain() -> None:
    """Let the started background job tasks run to completion."""
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


async def _conditional_claim(inner, job_id, runner_id, condition):  # type: ignore[no-untyped-def]
    async with await inner.get_service() as service:
        dto = inner.get_dto_type()
        return await service.update(
            dto.model_validate(
                {
                    "id": job_id,
                    "status": "RUNNING",
                    "claimed_by": runner_id,
                    "claimed_at": datetime.now(UTC),
                    "attempts": 1,
                }
            ),
            condition=condition,
        )


async def _force_running(inner, job_id, *, claimed_by, attempts, seconds_ago):  # type: ignore[no-untyped-def]
    """Force a row to ``RUNNING`` with a backdated ``claimed_at`` (raw SQL)."""
    async with await inner.get_service() as service:
        session = service._active_session()
        await session.execute(
            update(inner.table)
            .where(inner.id_column == job_id)
            .values(
                status=JobStatus.RUNNING,
                claimed_by=claimed_by,
                claimed_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
                attempts=attempts,
            )
        )
        await session.commit()


async def _backdate_claim(inner, job_id, *, seconds: int) -> None:
    async with await inner.get_service() as service:
        session = service._active_session()
        await session.execute(
            update(inner.table)
            .where(inner.id_column == job_id)
            .values(claimed_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await session.commit()


# ---------------------------------------------------------------------------
# RBAC over REST: a creator reads/edits only its own jobs; an admin reads all
# ---------------------------------------------------------------------------


class Role(AppRole):
    ADMIN = "ADMIN"
    USER = "USER"


ADMIN_ID = UUID("00000000-0000-0000-0000-0000000000ad")
ALICE_ID = UUID("11111111-1111-1111-1111-111111111111")
BOB_ID = UUID("22222222-2222-2222-2222-222222222222")

ADMIN_KEY = "admin-key"
ALICE_KEY = "alice-key"
BOB_KEY = "bob-key"

# Jobs are owner-scoped on ``creator_id`` for a USER; an ADMIN sees all. Reads
# are public by default here so an un-roled caller still reads (the union model
# means the Owner grant only ever *adds*).
JOBS_POLICIES = RolePolicyResolver(
    role_policies={Role.ADMIN: [AllowAll()]},
    resource_role_policies={"jobs": {Role.USER: [Owner(owner_field="creator_id")]}},
    default=[],
)


def _keys() -> ApiKeysConfig:
    return ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(
                id="admin", key=SecretStr(ADMIN_KEY), principal_id=str(ADMIN_ID), roles=["ADMIN"]
            ),
            ApiKeyConfig(
                id="alice", key=SecretStr(ALICE_KEY), principal_id=str(ALICE_ID), roles=["USER"]
            ),
            ApiKeyConfig(
                id="bob", key=SecretStr(BOB_KEY), principal_id=str(BOB_ID), roles=["USER"]
            ),
        ]
    )


@pytest_asyncio.fixture
async def jobs_client(jobs) -> AsyncIterator[AsyncClient]:
    """A REST client over the jobs view, with the owner-scoping policy wired."""
    _maker, inner = jobs
    view = jobs_view(inner)
    key_inner = config_api_key_resource(_keys())
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner),
        policy_resolver=JOBS_POLICIES,
        posture=Posture.OPTIONAL,
    )
    manifest = Manifest(resources=[view, config_api_key_view(key_inner)])
    app = create_app(manifest, dependency_builder=builder)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_creator_reads_only_its_own_jobs_and_admin_reads_all(jobs_client):
    alice = await jobs_client.post(
        "/jobs",
        json={"job_details": {"kind": "LogJobDetails", "message": "alice"}},
        headers=_auth(ALICE_KEY),
    )
    assert alice.status_code == 201
    bob = await jobs_client.post(
        "/jobs",
        json={"job_details": {"kind": "LogJobDetails", "message": "bob"}},
        headers=_auth(BOB_KEY),
    )
    assert bob.status_code == 201

    # Alice sees only her own job.
    listing = await jobs_client.get("/jobs", headers=_auth(ALICE_KEY))
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [i["job_details"]["message"] for i in items] == ["alice"]

    # Bob reading Alice's job by id is a 404 (existence not leaked).
    forbidden = await jobs_client.get(f"/jobs/{alice.json()['id']}", headers=_auth(BOB_KEY))
    assert forbidden.status_code == 404

    # The admin sees both.
    admin = await jobs_client.get("/jobs", headers=_auth(ADMIN_KEY))
    messages = sorted(i["job_details"]["message"] for i in admin.json()["items"])
    assert messages == ["alice", "bob"]


async def test_the_creator_field_is_stamped_and_not_client_writable(jobs_client):
    resp = await jobs_client.post(
        "/jobs",
        json={
            "job_details": {"kind": "LogJobDetails", "message": "x"},
            "creator_id": str(BOB_ID),
        },
        headers=_auth(ALICE_KEY),
    )
    # creator_id is not a client field, so the stray value is ignored; the row
    # is stamped with Alice's id and Alice can read it back.
    assert resp.status_code == 201
    read = await jobs_client.get(f"/jobs/{resp.json()['id']}", headers=_auth(ALICE_KEY))
    assert read.status_code == 200
