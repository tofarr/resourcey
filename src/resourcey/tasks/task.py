"""Background tasks and their polymorphic configuration (issue #15).

A :class:`BackgroundTask` is a named, async callable — ``async def __call__`` —
whose schedule and enabled flag come from a **polymorphic config value object**
injected through its constructor. Both are
:class:`~resourcey.util.models.DiscriminatedUnionMixin` subclasses keyed by
``kind`` (the class name), so a deployment can select a concrete config or task
by name without importing every subclass.

The central block is :class:`BackgroundTasksConfig`: a
:class:`~resourcey.config.config_base.BaseConfig` carrying a ``timezone``, a
``scheduler_enabled`` toggle, and the list of per-task configs. The app's main
file constructs each task with its matched config entry
(``config.for_task("digest", SendDigestConfig)``) and hands the tasks to
:class:`~resourcey.tasks.scheduler.BackgroundTaskScheduler`, a ``Manifest``
manager.

This module imports only lower framework layers (``config`` / ``util``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypeVar

from pydantic import Field

from resourcey.config.config_base import BaseConfig
from resourcey.util.models import DiscriminatedUnionMixin

if TYPE_CHECKING:
    from collections.abc import Sequence

TConfig = TypeVar("TConfig", bound="BackgroundTaskConfig")

# The default timezone a cron schedule is interpreted in.
DEFAULT_TIMEZONE = "UTC"


class BackgroundTaskConfig(DiscriminatedUnionMixin, ABC):
    """The configuration for one background task, extensible per task kind.

    Every task has a ``name`` and a ``schedule`` / ``enabled`` pair. A task kind
    that needs more declares its own subclass (e.g. ``SendDigestConfig``), so
    the central config stays open without a schema change.

    Attributes:
        name: The task's stable identifier — config lookup, run-by-name, and the
            log / telemetry label. Unique across an app's tasks.
        schedule: A 5-field cron expression (or an ``@``-shorthand); ``None``
            means the task has no tick and only runs by name.
        enabled: Whether the scheduler fires the task. Active by default, so a
            clustered deployment narrows this in the container(s) meant to run
            the job.
    """

    name: str = Field(description="The task's stable identifier (unique per app).")
    schedule: str | None = Field(
        default=None,
        description=(
            "A 5-field cron expression or an @-shorthand (e.g. '@daily'); None means "
            "the task has no scheduled tick and only runs by name."
        ),
    )
    enabled: bool = Field(
        default=True,
        description="Whether the scheduler fires this task (active by default).",
    )


class BackgroundTasksConfig(BaseConfig):
    """The central scheduling config: timezone, the scheduler toggle, and the tasks.

    Parsed under the process-wide prefix (``APP`` by default) as
    ``APP_BACKGROUND_TASKS_TIMEZONE`` / ``_SCHEDULER_ENABLED`` and a
    list-of-nested polymorphic entry per task:
    ``APP_BACKGROUND_TASKS_<n>_KIND`` (the dotted path of the config subclass),
    ``_NAME``, ``_SCHEDULE``, ``_ENABLED``, plus any kind-specific field — the
    same shape :class:`~resourcey.sql.sql_config.SqlConfig` uses for its
    connections.

    Attributes:
        background_tasks_timezone: The IANA timezone each cron schedule is
            interpreted in.
        background_tasks_scheduler_enabled: Whether this process starts the
            scheduler loop when the manifest is entered.
        background_tasks: The per-task config entries, in declaration order.
    """

    background_tasks_timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description="The IANA timezone each cron schedule is interpreted in.",
    )
    background_tasks_scheduler_enabled: bool = Field(
        default=True,
        description="Whether this process starts the scheduler loop when the manifest starts.",
    )
    background_tasks: list[BackgroundTaskConfig] = Field(
        default_factory=list,
        description="The per-task config entries, in declaration order.",
    )

    def get_task(self, name: str) -> BackgroundTaskConfig | None:
        """The config entry named ``name``, or ``None`` when there is none.

        A duplicate name would make the lookup ambiguous, so it is rejected here
        (the central config is the one place that sees every entry).
        """
        matches = [entry for entry in self.background_tasks if entry.name == name]
        if len(matches) > 1:
            raise ValueError(
                f"Duplicate background-task config for name {name!r}; task names must be unique."
            )
        return matches[0] if matches else None

    def for_task(self, name: str, kind: type[TConfig]) -> TConfig:
        """The task config for ``name``, coerced to ``kind``.

        Returns the matching entry when one exists — the entry must already be
        the expected ``kind``, so a mis-paired entry (a ``SendDigestConfig``
        under a ``Sweep``'s name) fails loudly rather than silently no-opping.
        With no entry, returns a defaults-only ``kind(name=name)``, so a task
        works unconfigured and is overridden when config exists.
        """
        entry = self.get_task(name)
        if entry is None:
            return kind(name=name)
        if not isinstance(entry, kind):
            raise ValueError(
                f"Background-task config for {name!r} is a {entry.kind}, but the task "
                f"expects {kind.__name__}."
            )
        return entry


class BackgroundTask(DiscriminatedUnionMixin, ABC):
    """A named, async unit of work, configured by an injected config object.

    The base is abstract; a concrete task subclasses it and implements
    :meth:`__call__`. ``config`` is a pydantic field, so a concrete task
    declares its config type and constructs itself with it, keeping any mutable
    runtime state in a :class:`~pydantic.PrivateAttr`::

        class SendDigest(BackgroundTask):
            config: SendDigestConfig
            _sent: int = PrivateAttr(default=0)

            def __init__(self, config: SendDigestConfig, *, mailer: Mailer) -> None:
                super().__init__(config=config)
                self._mailer = mailer

            async def __call__(self) -> None:
                await self._mailer.send(self.config.recipient)
                self._sent += 1

    The ``name`` / ``schedule`` / ``enabled`` the scheduler reads come from
    ``config``.
    """

    config: BackgroundTaskConfig

    @property
    def name(self) -> str:
        """The task's stable identifier (from its config)."""
        return self.config.name

    @property
    def schedule(self) -> str | None:
        """The task's cron expression, or ``None`` for a manual-only task."""
        return self.config.schedule

    @property
    def enabled(self) -> bool:
        """Whether the scheduler fires this task."""
        return self.config.enabled

    @abstractmethod
    async def __call__(self) -> None:
        """Run the task's work."""


def assert_unique_names(tasks: Sequence[BackgroundTask]) -> None:
    """Reject duplicate task names up front.

    A silent duplicate would make run-by-name and the config lookup ambiguous,
    so this is the loud-at-startup check the manifest's action assertion
    (:func:`~resourcey.core.service.assert_real_actions`) is for the route set.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for task in tasks:
        if task.name in seen:
            duplicates.append(task.name)
        seen.add(task.name)
    if duplicates:
        raise ValueError(
            f"Duplicate background-task name(s): {sorted(set(duplicates))}; "
            "task names must be unique."
        )
