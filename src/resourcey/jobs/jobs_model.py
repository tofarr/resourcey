"""The ``jobs`` table and its governed resources (issue #16).

The workflow is **model-first**, like every SQL resource: :class:`Job` is the ORM
model (the schema of record) and the framework infers the DTO / REST models from
it. A deployment points Alembic at :data:`JobsBase.metadata`.

The row is a **governed** entity: served over REST, RBAC-scoped, cached, and
migrated like any other resource. The runner-managed fields are hidden from the
public surface, which is a ``ResourceView`` (:func:`jobs_view`) that also narrows
``status``. The runner writes through the **inner** resource
(:func:`jobs_resource`).

This module is part of ``resourcey.jobs``; it imports lower framework layers
(``core`` / ``sql`` / ``view``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Uuid
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.auth.auth_principal import PRINCIPAL_CTX_KEY
from resourcey.core.dto import DtoField
from resourcey.core.resource import Resource
from resourcey.jobs.jobs_details import JobDetails
from resourcey.sql.model_type import ModelType
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


# Neither client-suppliable on create nor writable on update.
RUNNER_FIELD = DtoField(in_create_request=False, in_update_request=False)

# A client may enqueue, schedule, or cancel; ``RUNNING`` / ``COMPLETED`` /
# ``ERROR`` are runner-managed and rejected on the wire.
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
    """The declarative base owning the ``jobs`` table (an app points Alembic at it)."""


class Job(JobsBase):
    """A durable, claimable unit of work — a governed ``jobs`` row.

    ``job_details_kind`` is the discriminator the runner deserializes the body
    by, derived from the body on create. ``max_attempts`` defaults to ``1`` (no
    retry unless opted in), and ``max_seconds_for_run`` is both the wall-clock
    cap and the stale-claim bound.
    """

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_details_kind: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, info={"dto_field": RUNNER_FIELD}
    )
    job_details: Mapped[JobDetails] = mapped_column(
        ModelType(JobDetails), nullable=False, info={"dto_field": DtoField(in_update_request=False)}
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

    A "not terminal" condition is folded into every client update, so it is
    atomic with the write — an update of a terminal job is a no-op returning
    ``None`` (the same result as an absent row), never an error.
    """

    async def create(self, payload: Any) -> Any:
        """Derive ``job_details_kind`` and stamp ``creator_id`` on create.

        ``job_details_kind`` is derived from the body's own ``kind``.
        ``creator_id`` is stamped from the authenticated principal; the runner's
        ``enqueue`` passes it explicitly, since it serves no request.
        """
        updates: dict[str, Any] = {}
        details = getattr(payload, "job_details", None)
        if isinstance(details, JobDetails):
            updates["job_details_kind"] = type(details).__name__
        elif isinstance(details, dict) and isinstance(details.get("kind"), str):
            # Defensive: a raw mapping rather than a validated model instance.
            updates["job_details_kind"] = details["kind"]
        principal = self._ctx.get(PRINCIPAL_CTX_KEY)
        creator = getattr(payload, "creator_id", MISSING)
        if principal is not None and creator in (MISSING, None):
            updates["creator_id"] = principal.id
        if updates:
            payload = payload.model_copy(update=updates)
        return await super().create(payload)

    async def update(self, payload: Any, *, condition: SearchFilter[Any] | None = None) -> Any:
        """Apply the update, refusing to transition a **terminal** job."""
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

    The read model keeps the full ``JobStatus``, so a job the runner has taken
    to ``RUNNING`` / ``COMPLETED`` / ``ERROR`` still reads back correctly.
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
        exposed_request_type_overrides={"status": CLIENT_STATUS_TYPE},
    )
