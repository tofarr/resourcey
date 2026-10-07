"""Smoke tests for the durable-jobs example app.

These pin the factory wiring and the job kinds against a real, migrated SQLite
database, plus the public view's field / status narrowing. The end-to-end REST +
live-runner behaviour lives in ``test_e2e.py``.
"""

from __future__ import annotations

import pytest

from jobs_example.jobs import BoomJobDetails, EchoJobDetails, SlowJobDetails
from resourcey.jobs.jobs_details import JobDetails, JobRun
from resourcey.jobs.jobs_model import (
    CLIENT_STATUS_TYPE,
    JobStatus,
    jobs_resource,
    jobs_view,
)


async def test_app_exposes_the_jobs_resource(wired: tuple) -> None:
    app = wired[2]
    assert "/jobs" in app.openapi()["paths"]


def test_job_kinds_round_trip_through_the_discriminator() -> None:
    for cls, payload in [
        (EchoJobDetails, {"message": "hi"}),
        (SlowJobDetails, {"seconds": 0.1}),
        (BoomJobDetails, {"message": "x"}),
    ]:
        body = JobDetails.model_validate({"kind": cls.__name__, **payload})
        assert isinstance(body, cls)


async def test_echo_job_succeeds_and_echoes() -> None:
    run = await EchoJobDetails(message="hello")()
    assert run == JobRun(status="COMPLETED", detail="echoed: hello")


async def test_boom_job_raises() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        await BoomJobDetails()()


def test_public_view_hides_runner_managed_fields() -> None:
    inner = jobs_resource(session_factory=None)
    view = jobs_view(inner)
    read_fields = set(view.get_rest_models().read_response.model_fields)
    assert {"claimed_by", "claimed_at", "attempts"}.isdisjoint(read_fields)
    # The read model keeps the full status enum (the runner's RUNNING etc. read
    # back), while the create request narrows it to the client-writable values.
    read_status = view.get_rest_models().read_response.model_fields["status"].annotation
    assert read_status == JobStatus
    create_status = view.get_rest_models().create_request.model_fields["status"].annotation
    assert create_status == CLIENT_STATUS_TYPE
