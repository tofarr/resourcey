"""Durable-jobs example app entry point (issue #16).

This example shows a **durable, claimable** background-job framework on top of
:mod:`resourcey.jobs`. A request (or a scheduled task) enqueues a job; a pool of
workers claims and runs it **exactly once**, with retries and crash recovery —
none of which the in-process :mod:`resourcey.tasks` scheduler provides on its own.

What the app wires:

* the framework's ``jobs`` resource (the ``jobs`` table), exposed through its
  public :func:`~resourcey.jobs.jobs_model.jobs_view` — a governed resource with
  the runner-managed fields hidden and ``status`` narrowed to the client-writable
  set, served over the ordinary REST surface with RBAC;
* a :class:`~resourcey.jobs.jobs_runner.JobRunner` in the manifest's ``managers``
  slot, so its sweep loop is tied to the app's lifecycle;
* the example's job kinds (:mod:`jobs_example.jobs`) and a scheduled
  :class:`~jobs_example.tasks.EnqueueEchoTask` that enqueues a durable job every
  tick — the :mod:`resourcey.tasks` → :mod:`resourcey.jobs` bridge;
* the role-based API-key wiring from example 04, so enqueuing / cancelling is
  role-checked and a ``USER``'s jobs are scoped to it.

A client enqueues a job with an ordinary ``POST /jobs``::

    {"job_details": {"kind": "EchoJobDetails", "message": "hi"}}

The runner claims it on its next sweep, runs the body (holding no database
session), and writes the terminal ``COMPLETED`` / ``ERROR`` — all durable, so a
crash mid-run is recovered by the stale-claim sweep.

Run with::

    uvicorn jobs_example.app:app --env-file .env

Note the ``--env-file``: the framework does no ``.env`` loading of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from jobs_example.tasks import EnqueueEchoConfig, EnqueueEchoTask
from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_config import ApiKeysConfig
from resourcey.auth.auth_policy import AllowAll, Owner
from resourcey.auth.auth_role import AppRole, RolePolicyResolver
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.http.app import create_app
from resourcey.jobs.jobs_config import JobsConfig
from resourcey.jobs.jobs_model import jobs_resource, jobs_view
from resourcey.jobs.jobs_runner import JobRunner
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.tasks.scheduler import BackgroundTaskScheduler
from resourcey.tasks.task import BackgroundTasksConfig


class Role(AppRole):
    """This app's role vocabulary (per-app, like example 04)."""

    ADMIN = "ADMIN"
    USER = "USER"


# The single place the app expresses its role -> policy rules.
#
#   * ``jobs`` — a USER may enqueue, read, and cancel **its own** jobs: the
#     ``Owner`` policy scopes on ``creator_id`` (own rows for the read-like /
#     by-id write actions, unscoped create). An ADMIN has full access. Unlike
#     example 04's board, jobs are **not** public, so there is no resource-level
#     ``ReadOnly`` default — an un-roled / anonymous caller falls to the
#     fail-closed empty default and reaches nothing.
ROLE_POLICIES = RolePolicyResolver(
    role_policies={Role.ADMIN: [AllowAll()]},
    resource_role_policies={
        "jobs": {Role.USER: [Owner(owner_field="creator_id")]},
    },
    default=[],
)


# One manager for the whole app; the manifest's lifecycle enters it.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_auth(
    keys: ApiKeysConfig | None = None,
) -> tuple[AuthorizedDependencyBuilder, Resource[Any, Any]]:
    """The role-aware API-key builder plus the exposed key view to register."""
    api_keys = keys if keys is not None else ApiKeysConfig.get_instance()
    key_inner: Resource[Any, Any] = config_api_key_resource(api_keys)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner),
        policy_resolver=ROLE_POLICIES,
        posture=Posture.OPTIONAL,
    )
    return builder, config_api_key_view(key_inner)


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    keys: ApiKeysConfig | None = None,
    jobs_config: JobsConfig | None = None,
    tasks_config: BackgroundTasksConfig | None = None,
    runner_enabled: bool = True,
    scheduler_enabled: bool = True,
) -> tuple[Manifest, FastAPI, JobRunner]:
    """Build the manifest, the FastAPI app, and the job runner.

    A factory so tests can inject their own ``session_manager`` (an isolated
    database), ``keys``, config, and enablement flags. The runner is always
    registered as a manifest manager, but its sweep loop only starts when
    enabled (the config gate), so a web replica can run the app without claiming
    jobs.
    """
    manager = session_manager or default_session_manager

    # The **inner** jobs resource is what the runner writes through; the view is
    # what the REST surface registers.
    inner_jobs = jobs_resource(session_manager=manager)

    runner = JobRunner(
        inner_jobs,
        config=jobs_config if jobs_config is not None else JobsConfig.get_instance(),
        enabled=runner_enabled,
    )

    # A scheduled task that enqueues a durable job every tick (the tasks -> jobs
    # bridge). Its config comes from APP_BACKGROUND_TASKS_*.
    tasks_cfg = tasks_config if tasks_config is not None else BackgroundTasksConfig.get_instance()
    echo_task = EnqueueEchoTask(
        tasks_cfg.for_task("enqueue-echo", EnqueueEchoConfig), runner=runner
    )
    scheduler = BackgroundTaskScheduler(
        [echo_task],
        config=tasks_cfg.model_copy(
            update={"background_tasks_scheduler_enabled": scheduler_enabled}
        ),
    )

    builder, key_view = build_auth(keys)
    manifest = Manifest(
        resources=[jobs_view(inner_jobs), key_view],
        # The manager lifecycle enters the session manager, the runner's sweep
        # loop, and the task scheduler — in that order, and exited in reverse.
        managers=[manager, runner, scheduler],
    )
    return manifest, create_app(manifest, dependency_builder=builder), runner


manifest, app, runner = build_app()
