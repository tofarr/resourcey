"""The ``jobs`` table and its governed resources (issue #16).

The workflow is **model-first**, like every SQL resource: :class:`Job` is the
ORM model (the schema of record) and the framework infers the DTO / REST models
from it. A deployment points Alembic at :data:`JobsBase.metadata` (or imports the
model) exactly as it does for any other SQLAlchemy model.

The row is a **governed** entity: it is served over REST, RBAC-scoped, cached,
and migrated like any other resource. Three of its fields are runner-managed —
``claimed_by`` / ``claimed_at`` / ``attempts`` — and its ``status`` has a
restricted client-writable set, so the public surface is a
:class:`~resourcey.view.resource_view.ResourceView` (:func:`jobs_view`) that
hides the runner-managed fields and narrows ``status``. The runner writes through
the **inner** resource (:func:`jobs_resource`), which carries the full model.

This module is part of ``resourcey.jobs``; it imports lower framework layers
(``core`` / ``sql`` / ``view``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Integer, String, Uuid
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_principal import PRINCIPAL_CTX_KEY
from resourcey.core.dto import DtoField
from resourcey.core.resource import Resource
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_resource import SqlResource
from resourcey.sql.sql_service import SqlService
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import (
    AttrFilter,
    EqFilter,
    SearchFilter,
    and_,
    not_,
    or_,
)
from resourcey.view.resource_view import ResourceView


def utc_now() -> datetime:
    """Default factory for the ``jobs`` timestamp columns."""
    return datetime.now(UTC)


# A runner-managed field: neither client-suppliable on create nor writable on
# update. Declared on the column's ``info`` (the documented escape hatch).
RUNNER_FIELD = DtoField(in_create_request=False, in_update_request=False)

# The values a client may write to ``status`` on the public surface: it may
# enqueue (``PENDING``), schedule a future one-shot (``SCHEDULED``), or cancel
# (``CANCELLED``). ``RUNNING`` / ``COMPLETED`` / ``ERROR`` are runner-managed and
# rejected on the wire — the public view narrows the request models to this set.
CLIENT_STATUS_VALUES: tuple[str, ...] = ("PENDING", "SCHEDULED", "CANCELLED")

# The client-writable status type: a ``Literal`` (not the full ``JobStatus``
# enum) so the view's request models accept only these values.
CLIENT_STATUS_TYPE = Literal["PENDING", "SCHEDULED", "CANCELLED"]


class JobStatus(StrEnum):
    """A job's lifecycle status.

    ``PENDING`` and ``SCHEDULED`` are the non-terminal, claimable states;
    ``RUNNING`` is the claimed, in-flight state; ``COMPLETED`` / ``ERROR`` /
    ``CANCELLED`` are **terminal** and never transition.
    """

    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


# The terminal statuses: a job in one of these never transitions again. Sticky
# so a late completion (or a recovered claim) can never overwrite a terminal row.
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED}
)


class JobsBase(DeclarativeBase):
    """The declarative base owning the ``jobs`` table.

    Kept local so the jobs package is self-contained; an app points Alembic at
    this metadata (or imports the model) exactly as it does for any SQLAlchemy
    model.
    """


class Job(JobsBase):
    """A durable, claimable unit of work — a governed ``jobs`` row.

    Attributes:
        id: The server-generated identifier.
        job_details_kind: The concrete ``JobDetails`` kind (its class name); the
            discriminator the runner deserializes the body by. Runner-managed
            (derived from the body on create).
        job_details: The serialized ``JobDetails`` body (JSON). Immutable once
            created.
        status: The lifecycle status (see :class:`JobStatus`). A client may set
            only :data:`CLIENT_STATUS_VALUES`; the rest are runner-managed.
        detail: Optional human-readable status text.
        run_at: For a ``SCHEDULED`` job, when it becomes claimable; ``NULL``
            means "as soon as claimed".
        claimed_by: The runner UUID holding the claim; ``NULL`` = unclaimed.
            Runner-managed.
        claimed_at: The claim start; the stale-claim sweep measures from here.
            Runner-managed.
        attempts: How many times the job has been claimed. Runner-managed.
        max_attempts: The retry cap; a job requeues while
            ``attempts < max_attempts``, else goes terminal. Defaults to ``1``
            (no retry unless opted in).
        max_seconds_for_run: An optional per-job wall-clock cap and the
            stale-claim bound; ``NULL`` falls back to the config default.
        creator_id: The owner the job is scoped to (the ``Owner`` policy's
            field). Runner-managed (stamped from the authenticated principal on
            create).
        created_at / updated_at: Framework-owned timestamps.
    """

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_details_kind: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, info={"dto_field": RUNNER_FIELD}
    )
    job_details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, info={"dto_field": DtoField(in_update_request=False)}
    )
    status: Mapped[JobStatus] = mapped_column(
        SqlEnum(JobStatus, native_enum=False, length=32),
        nullable=False,
        info={"dto_field": DtoField(default_for_create=JobStatus.PENDING)},
    )
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True, info={"dto_field": RUNNER_FIELD}
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info={"dto_field": RUNNER_FIELD}
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, info={"dto_field": RUNNER_FIELD}
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, info={"dto_field": DtoField(default_for_create=1)}
    )
    max_seconds_for_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True, info={"dto_field": RUNNER_FIELD}
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class JobsService(SqlService[Any, Any]):
    """A ``jobs`` service that enforces the **terminal-status** rule on writes.

    A job in a terminal status (``COMPLETED`` / ``ERROR`` / ``CANCELLED``) never
    transitions: a client may cancel a ``PENDING`` / ``SCHEDULED`` job but may not
    revive a finished one, and the runner's own writes already carry a
    ``status == RUNNING`` condition. The rule is enforced by folding a
    "not terminal" condition into every client update, so it is atomic with the
    write — an update of a terminal job is a no-op returning ``None`` (the same
    result as an absent row), never an error.
    """

    async def create(self, payload: Any) -> Any:
        """Derive ``job_details_kind`` and stamp ``creator_id`` on create.

        ``job_details_kind`` is not a client field: it is the discriminator the
        runner deserializes the body by, so it is derived from the body's own
        ``kind``. ``creator_id`` is likewise runner-managed (the ``Owner`` policy
        leaves ``create`` unscoped, so a new row has no owner until here); the
        runner's own :meth:`~resourcey.jobs.jobs_runner.JobRunner.enqueue` passes
        it explicitly (it is not serving a request, so there is no principal on
        the ctx).
        """
        updates: dict[str, Any] = {}
        details = getattr(payload, "job_details", None)
        if isinstance(details, dict) and isinstance(details.get("kind"), str):
            updates["job_details_kind"] = details["kind"]
        principal = self._ctx.get(PRINCIPAL_CTX_KEY)
        creator = getattr(payload, "creator_id", MISSING)
        if principal is not None and creator in (MISSING, None):
            updates["creator_id"] = principal.id
        if updates:
            payload = payload.model_copy(update=updates)
        return await super().create(payload)

    async def update(self, payload: Any, *, condition: SearchFilter[Any] | None = None) -> Any:
        """Apply the update, refusing to transition a **terminal** job.

        A job in ``COMPLETED`` / ``ERROR`` / ``CANCELLED`` never transitions, so a
        "not terminal" guard is folded into the write's condition: an update of a
        terminal job is a no-op returning ``None`` (the same result as an absent
        row), never an error. The runner's own writes already carry a
        ``status == RUNNING`` condition, so this is the client-facing half of the
        rule.
        """
        terminal = or_(
            *(
                AttrFilter(attribute="status", filter=EqFilter(value=status))
                for status in TERMINAL_STATUSES
            )
        )
        guard = not_(terminal)
        folded = and_(guard, condition) if condition is not None else guard
        return await super().update(payload, condition=folded)


class JobsResource(SqlResource[Any, Any]):
    """The ``jobs`` resource, whose service enforces the terminal-status rule."""

    def make_service(self, ctx: Any, session_factory: Any) -> Any:
        return JobsService(self, ctx, session_factory)


def jobs_resource(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
    path: str = "jobs",
) -> Resource[Any, Any]:
    """The **inner** ``jobs`` resource: the full row, for the runner / RBAC.

    The runner writes through this (it is not serving a request, so no
    ``AuthorizedService`` policy scope is folded into its conditional writes).
    Register :func:`jobs_view` over it, not this, for the public surface.
    """
    return JobsResource(
        Job,
        session_manager=session_manager,
        session_factory=session_factory,
        path=path,
    )


def jobs_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed ``jobs`` view: hides runner-managed fields, narrows ``status``.

    It hides ``claimed_by`` / ``claimed_at`` / ``attempts`` (runner-managed, never
    client-visible or client-writable) and narrows ``status``'s *request* type to
    :data:`CLIENT_STATUS_VALUES`, so a client cannot write a runner-managed
    status. The read model keeps the full :class:`JobStatus`, so a job the runner
    has taken to ``RUNNING`` / ``COMPLETED`` / ``ERROR`` still reads back
    correctly.
    """
    hidden = {
        "in_read_response": False,
        "in_search_response": False,
        "in_create_response": False,
        "in_update_response": False,
    }
    return ResourceView(
        inner,
        exposed_field_overrides={
            "claimed_by": dict(hidden),
            "claimed_at": dict(hidden),
            "attempts": dict(hidden),
        },
        exposed_type_overrides={"status": CLIENT_STATUS_TYPE},
    )
