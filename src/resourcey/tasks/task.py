"""Background tasks and their polymorphic configuration (issue #15).

A :class:`BackgroundTask` is a named, async callable whose schedule and enabled
flag come from a **polymorphic config value object** injected through its
constructor. Both are ``DiscriminatedUnionMixin`` subclasses keyed by ``kind``,
so a deployment can select a concrete config or task by name without importing
every subclass.

:class:`BackgroundTasksConfig` carries the timezone, the scheduler toggle, and the
list of per-task configs. The app constructs each task with its matched config
entry and hands the tasks to ``BackgroundTaskScheduler``, a ``Manifest`` manager.

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

    A task kind that needs more declares its own subclass, keeping the central
    config open without a schema change.
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

    Parsed as ``APP_BACKGROUND_TASKS_TIMEZONE`` / ``_SCHEDULER_ENABLED`` and a
    list-of-nested polymorphic entry per task (``APP_BACKGROUND_TASKS_<n>_KIND``
    / ``_NAME`` / ``_SCHEDULE`` / ``_ENABLED`` plus any kind-specific field).
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

        A duplicate name is rejected here (the one place that sees every entry).
        """
        matches = [entry for entry in self.background_tasks if entry.name == name]
        if len(matches) > 1:
            raise ValueError(
                f"Duplicate background-task config for name {name!r}; task names must be unique."
            )
        return matches[0] if matches else None

    def for_task(self, name: str, kind: type[TConfig]) -> TConfig:
        """The task config for ``name``, coerced to ``kind``.

        A mis-paired entry fails loudly. With no entry, returns a defaults-only
        ``kind(name=name)``, so a task works unconfigured.
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

    A concrete task declares its config type as a pydantic field and keeps any
    mutable runtime state in a :class:`~pydantic.PrivateAttr`. The ``name`` /
    ``schedule`` / ``enabled`` the scheduler reads come from ``config``.
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
    """Reject duplicate task names up front, so run-by-name stays unambiguous."""
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
