"""Tests for the background-task framework (issue #15).

Exercised against the **real** production code paths (no mocks): a real
:class:`BackgroundTask`, a real :class:`BackgroundTaskScheduler` manager entered
through a real :class:`~resourcey.core.manifest.Manifest`, and the real cron
parser. Time is controlled by calling :meth:`BackgroundTaskScheduler.tick` with
a chosen instant rather than by faking a clock.

Covered: cron next-fire (incl. the six common patterns and the DOM/DOW OR rule),
invalid-expression rejection, polymorphic config resolution, duplicate-name
rejection, run-by-name (incl. a disabled task), the ``scheduler_enabled`` gate,
a raising task not stopping the loop, overlapping ticks, and the CLI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import PrivateAttr

from resourcey.core.manifest import Manifest
from resourcey.tasks import cli as tasks_cli
from resourcey.tasks.cron import CronError, parse_cron
from resourcey.tasks.scheduler import BackgroundTaskScheduler, scheduler_from_manifest
from resourcey.tasks.task import (
    BackgroundTask,
    BackgroundTaskConfig,
    BackgroundTasksConfig,
    assert_unique_names,
)

# ---------------------------------------------------------------------------
# Test doubles (real subclasses of the real base — no mocking)
# ---------------------------------------------------------------------------


class TickConfig(BackgroundTaskConfig):
    """A config with a kind-specific field, for the polymorphic-resolution tests."""

    marker: str = "default"


class OtherConfig(BackgroundTaskConfig):
    """A different config kind, to prove a mis-paired entry is rejected."""


class Tick(BackgroundTask):
    """A task that records how many times it ran."""

    config: TickConfig
    _calls: int = PrivateAttr(default=0)

    async def __call__(self) -> None:
        self._calls += 1


class Boom(BackgroundTask):
    """A task that always raises, to prove the loop survives a failure."""

    config: TickConfig
    _calls: int = PrivateAttr(default=0)

    async def __call__(self) -> None:
        self._calls += 1
        raise RuntimeError("boom")


def _config(**kwargs: object) -> BackgroundTasksConfig:
    """A config with the scheduler enabled unless a test says otherwise."""
    return BackgroundTasksConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cron next-fire
# ---------------------------------------------------------------------------

# 2026-03-14 10:00 UTC is a Saturday.
_BASE = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # The six common patterns from the issue.
        ("* * * * *", "2026-03-14 10:01"),
        ("*/5 * * * *", "2026-03-14 10:05"),
        ("0 * * * *", "2026-03-14 11:00"),
        ("@hourly", "2026-03-14 11:00"),
        ("0 0 * * *", "2026-03-15 00:00"),
        ("@daily", "2026-03-15 00:00"),
        ("0 0 * * 0", "2026-03-15 00:00"),
        ("@weekly", "2026-03-15 00:00"),
        ("0 0 1 * *", "2026-04-01 00:00"),
        ("@monthly", "2026-04-01 00:00"),
        ("@yearly", "2027-01-01 00:00"),
        # Names, ranges, steps over ranges, lists, and 7-as-Sunday.
        ("0 9 * * mon", "2026-03-16 09:00"),
        ("30 8-17 * * *", "2026-03-14 10:30"),
        ("0 0 */2 * *", "2026-03-15 00:00"),
        ("0 0 1,15 * *", "2026-03-15 00:00"),
        ("0 0 * * 7", "2026-03-15 00:00"),
        ("@annually", "2027-01-01 00:00"),
        ("@midnight", "2026-03-15 00:00"),
    ],
)
def test_next_fire_common_patterns(expression: str, expected: str) -> None:
    got = parse_cron(expression).next_fire(_BASE).strftime("%Y-%m-%d %H:%M")
    assert got == expected


def test_next_fire_is_strictly_after() -> None:
    # An every-minute schedule never returns the instant it was given.
    schedule = parse_cron("* * * * *")
    assert schedule.next_fire(_BASE) > _BASE
    # And a schedule matching the given minute still advances past it.
    at_noon = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    assert parse_cron("0 12 * * *").next_fire(at_noon) == datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


def test_day_of_month_and_weekday_both_restricted_is_an_or() -> None:
    # The 13th OR Friday: from March 1 the first match is Friday the 6th.
    schedule = parse_cron("0 0 13 * 5")
    assert schedule.next_fire(datetime(2026, 3, 1, tzinfo=UTC)) == datetime(
        2026, 3, 6, 0, 0, tzinfo=UTC
    )
    # A non-Friday 13th still matches via the day-of-month branch alone.
    assert schedule.matches(datetime(2026, 5, 13, 0, 0, tzinfo=UTC))
    # And a Friday that is not the 13th matches via the weekday branch alone.
    assert schedule.matches(datetime(2026, 3, 6, 0, 0, tzinfo=UTC))
    # A non-Friday, non-13th day matches neither.
    assert not schedule.matches(datetime(2026, 5, 12, 0, 0, tzinfo=UTC))


def test_only_one_of_dom_dow_restricted_must_match() -> None:
    # day-of-month fixed, weekday '*': only the day matters.
    assert parse_cron("0 0 13 * *").matches(datetime(2026, 5, 13, 0, 0, tzinfo=UTC))
    assert not parse_cron("0 0 13 * *").matches(datetime(2026, 5, 12, 0, 0, tzinfo=UTC))
    # weekday fixed, day-of-month '*': only the weekday matters.
    assert parse_cron("0 0 * * 5").matches(datetime(2026, 5, 15, 0, 0, tzinfo=UTC))
    assert not parse_cron("0 0 * * 5").matches(datetime(2026, 5, 14, 0, 0, tzinfo=UTC))


def test_matches_rejects_a_wrong_minute_or_hour_or_month() -> None:
    schedule = parse_cron("0 12 1 6 *")
    assert schedule.matches(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 6, 1, 12, 1, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 6, 1, 13, 0, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 7, 1, 12, 0, tzinfo=UTC))


def test_an_unsatisfiable_schedule_raises_rather_than_hanging() -> None:
    # February 30 never occurs, so the roll-forward exhausts and raises.
    with pytest.raises(CronError, match="Could not find a next fire"):
        parse_cron("0 0 30 2 *").next_fire(datetime(2026, 1, 1, tzinfo=UTC))


def test_empty_entry_in_a_field_is_rejected() -> None:
    with pytest.raises(CronError, match="Empty entry"):
        parse_cron("1,,2 * * * *")


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "5/2 * * * *",
        "10-5 * * * *",
        "@nope",
        "@reboot",
        "abc * * * *",
    ],
)
def test_invalid_expression_is_rejected(expression: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expression)


# ---------------------------------------------------------------------------
# Config resolution + uniqueness
# ---------------------------------------------------------------------------


def test_for_task_resolves_a_matching_entry() -> None:
    config = _config(background_tasks=[TickConfig(name="digest", schedule="@daily", marker="hi")])
    resolved = config.for_task("digest", TickConfig)
    assert isinstance(resolved, TickConfig)
    assert resolved.marker == "hi"
    assert resolved.schedule == "@daily"


def test_for_task_falls_back_to_defaults() -> None:
    config = _config()
    resolved = config.for_task("unconfigured", TickConfig)
    assert resolved.name == "unconfigured"
    assert resolved.schedule is None
    assert resolved.enabled is True


def test_for_task_rejects_a_mispaired_kind() -> None:
    config = _config(background_tasks=[TickConfig(name="digest")])
    with pytest.raises(ValueError, match="expects OtherConfig"):
        config.for_task("digest", OtherConfig)


def test_duplicate_config_names_are_rejected() -> None:
    config = _config(background_tasks=[TickConfig(name="dup"), TickConfig(name="dup")])
    with pytest.raises(ValueError, match="Duplicate"):
        config.get_task("dup")


def test_duplicate_task_names_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        assert_unique_names([Tick(config=TickConfig(name="a")), Tick(config=TickConfig(name="a"))])


def test_scheduler_rejects_duplicate_names() -> None:
    tasks = [Tick(config=TickConfig(name="a")), Tick(config=TickConfig(name="a"))]
    with pytest.raises(ValueError, match="Duplicate"):
        BackgroundTaskScheduler(tasks, config=_config())


def test_invalid_schedule_fails_when_the_scheduler_is_constructed() -> None:
    tasks = [Tick(config=TickConfig(name="a", schedule="not a cron"))]
    with pytest.raises(CronError):
        BackgroundTaskScheduler(tasks, config=_config())


# ---------------------------------------------------------------------------
# Run-by-name (independent of scheduling)
# ---------------------------------------------------------------------------


async def test_run_once_by_name_runs_even_a_disabled_task() -> None:
    task = Tick(config=TickConfig(name="manual", enabled=False))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    await scheduler.run_once("manual")
    assert task._calls == 1


async def test_run_once_without_a_name_runs_enabled_tasks_only() -> None:
    enabled = Tick(config=TickConfig(name="on"))
    disabled = Tick(config=TickConfig(name="off", enabled=False))
    scheduler = BackgroundTaskScheduler([enabled, disabled], config=_config())
    await scheduler.run_once()
    assert enabled._calls == 1
    assert disabled._calls == 0


async def test_run_once_unknown_name_raises() -> None:
    scheduler = BackgroundTaskScheduler([Tick(config=TickConfig(name="a"))], config=_config())
    with pytest.raises(KeyError, match="No background task named"):
        await scheduler.run_once("missing")


async def test_a_schedule_none_task_never_ticks() -> None:
    # No schedule: it is never in _upcoming, and tick() never starts it...
    task = Tick(config=TickConfig(name="manual", schedule=None))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    assert scheduler._upcoming(datetime.now(UTC)) == []
    scheduler.tick(datetime(2026, 3, 14, 10, 0, tzinfo=UTC))
    await asyncio.sleep(0)
    assert task._calls == 0
    # ...but it still runs by name.
    await scheduler.run_once("manual")
    assert task._calls == 1


# ---------------------------------------------------------------------------
# The tick gate (deterministic: no sleeping to a real minute boundary)
# ---------------------------------------------------------------------------


async def test_tick_starts_only_matching_active_tasks() -> None:
    every_minute = Tick(config=TickConfig(name="every", schedule="* * * * *"))
    at_noon = Tick(config=TickConfig(name="noon", schedule="0 12 * * *"))
    disabled = Tick(config=TickConfig(name="off", schedule="* * * * *", enabled=False))
    scheduler = BackgroundTaskScheduler([every_minute, at_noon, disabled], config=_config())

    # 10:00 -> only the every-minute task matches.
    scheduler.tick(datetime(2026, 3, 14, 10, 0, tzinfo=UTC))
    await asyncio.sleep(0)
    assert (every_minute._calls, at_noon._calls, disabled._calls) == (1, 0, 0)

    # 12:00 -> both active scheduled tasks match.
    scheduler.tick(datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
    await asyncio.sleep(0)
    assert (every_minute._calls, at_noon._calls, disabled._calls) == (2, 1, 0)


async def test_a_raising_task_does_not_stop_the_loop() -> None:
    boom = Boom(config=TickConfig(name="boom", schedule="* * * * *"))
    tick = Tick(config=TickConfig(name="tick", schedule="* * * * *"))
    scheduler = BackgroundTaskScheduler([boom, tick], config=_config())

    scheduler.tick(datetime(2026, 3, 14, 10, 0, tzinfo=UTC))
    await asyncio.sleep(0)
    # The raising task was attempted, and the other task still ran.
    assert boom._calls == 1
    assert tick._calls == 1

    # And the next tick still starts both.
    scheduler.tick(datetime(2026, 3, 14, 10, 1, tzinfo=UTC))
    await asyncio.sleep(0)
    assert boom._calls == 2
    assert tick._calls == 2


# ---------------------------------------------------------------------------
# The manager lifecycle
# ---------------------------------------------------------------------------


async def test_entering_a_disabled_scheduler_starts_no_loop() -> None:
    task = Tick(config=TickConfig(name="a", schedule="* * * * *"))
    scheduler = BackgroundTaskScheduler(
        [task], config=_config(background_tasks_scheduler_enabled=False)
    )
    async with scheduler:
        assert scheduler.entered
        assert scheduler._loop is None
    assert not scheduler.entered


async def test_entering_the_scheduler_starts_and_cancels_the_loop() -> None:
    task = Tick(config=TickConfig(name="a", schedule="* * * * *"))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    async with scheduler:
        assert scheduler._loop is not None
    assert scheduler._loop is None


async def test_scheduler_runs_as_a_manifest_manager() -> None:
    task = Tick(config=TickConfig(name="a", schedule="* * * * *"))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    manifest = Manifest(resources=[], managers=[scheduler])
    async with manifest:
        assert scheduler.entered
        assert scheduler_from_manifest(manifest) is scheduler
        scheduler.tick(datetime(2026, 3, 14, 10, 0, tzinfo=UTC))
        await asyncio.sleep(0)
    assert task._calls == 1


async def test_entering_the_scheduler_twice_raises() -> None:
    task = Tick(config=TickConfig(name="a", schedule="* * * * *"))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    async with scheduler:
        with pytest.raises(RuntimeError, match="already entered"):
            await scheduler.__aenter__()


async def test_the_loop_idles_when_no_task_is_scheduled() -> None:
    # A task with no schedule and a disabled one leave nothing to tick.
    manual = Tick(config=TickConfig(name="manual", schedule=None))
    disabled = Tick(config=TickConfig(name="off", schedule="* * * * *", enabled=False))
    scheduler = BackgroundTaskScheduler([manual, disabled], config=_config())
    async with scheduler:
        assert scheduler._loop is not None
        # The loop returns on its own once it sees nothing to schedule.
        await asyncio.sleep(0.05)
        assert scheduler._loop.done()
    assert manual._calls == 0 and disabled._calls == 0


def test_scheduler_from_manifest_without_a_scheduler_raises() -> None:
    with pytest.raises(ValueError, match="No BackgroundTaskScheduler"):
        scheduler_from_manifest(Manifest(resources=[], managers=[]))


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class _App:
    """A module-like holder so the CLI's import-path lookup has something to find."""

    manifest: Manifest


def _app_with_scheduler() -> tuple[Manifest, Tick]:
    task = Tick(config=TickConfig(name="digest", schedule="@daily"))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    manifest = Manifest(resources=[], managers=[scheduler])
    _App.manifest = manifest
    return manifest, task


def test_cli_list_prints_each_task() -> None:
    manifest, _ = _app_with_scheduler()
    scheduler = scheduler_from_manifest(manifest)
    listing = tasks_cli.format_tasks(scheduler)
    assert "digest" in listing
    assert "@daily" in listing
    assert "enabled" in listing


async def test_cli_run_one_does_not_start_the_loop() -> None:
    import sys

    manifest, task = _app_with_scheduler()
    sys.modules["_bg_app"] = _App  # type: ignore[assignment]
    try:
        args = _parse(["run", "_bg_app:manifest", "digest"])
        code = await tasks_cli._run(args)
    finally:
        del sys.modules["_bg_app"]
    assert code == 0
    assert task._calls == 1
    # The loop was never started: run_once does not enter the scheduler.
    assert scheduler_from_manifest(manifest).entered is False


class _Recorder:
    """A stand-in manager that records whether it was entered."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _Recorder:
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


async def test_cli_run_enters_other_managers_but_not_the_scheduler() -> None:
    import sys

    task = Tick(config=TickConfig(name="digest", schedule="@daily"))
    scheduler = BackgroundTaskScheduler([task], config=_config())
    recorder = _Recorder()
    _App.manifest = Manifest(resources=[], managers=[scheduler, recorder])
    sys.modules["_bg_app3"] = _App  # type: ignore[assignment]
    try:
        args = _parse(["run", "_bg_app3:manifest", "digest"])
        code = await tasks_cli._run(args)
    finally:
        del sys.modules["_bg_app3"]
    assert code == 0
    assert task._calls == 1
    # The other manager was entered and exited; the scheduler's loop never was.
    assert recorder.entered and recorder.exited
    assert scheduler.entered is False


def test_main_dispatches_tasks_list(capsys: pytest.CaptureFixture[str]) -> None:
    import sys

    from resourcey.__main__ import main

    _app_with_scheduler()
    sys.modules["_bg_app2"] = _App  # type: ignore[assignment]
    try:
        code = main(["tasks", "list", "_bg_app2:manifest"])
    finally:
        del sys.modules["_bg_app2"]
    assert code == 0
    assert "digest" in capsys.readouterr().out


def test_main_with_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    from resourcey.__main__ import main

    assert main([]) == 0
    assert "resourcey" in capsys.readouterr().out


def test_load_manifest_rejects_a_non_manifest() -> None:
    import sys
    import types

    class NotAManifest:
        pass

    module = types.ModuleType("_not_manifest")
    module.NotAManifest = NotAManifest  # type: ignore[attr-defined]
    sys.modules["_not_manifest"] = module
    try:
        with pytest.raises(TypeError, match="not a Manifest"):
            tasks_cli.load_manifest("_not_manifest:NotAManifest")
    finally:
        del sys.modules["_not_manifest"]


def _parse(argv: list[str]):
    """Parse a ``tasks`` argv through the real top-level dispatcher."""
    from resourcey.__main__ import _build_parser

    return _build_parser().parse_args(["tasks", *argv])
