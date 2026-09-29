"""``python -m resourcey tasks …`` — the background-task entry point (issue #15).

Loads an app's ``Manifest`` by a uvicorn-style ``module:attr`` spec and can:

* ``list`` every task with its cron schedule and enabled flag;
* ``run`` one task by name, or all of them, **once**.

A one-shot ``run`` does **not** enter the manifest's own lifespan, so the
scheduler's periodic loop never starts. It does enter the manifest's other
managers (so a SQL / Mongo-backed task finds a live engine), then runs and
exits — the external-cron / ``kubectl`` path.

The framework does no ``.env`` loading of its own, so run it as
``uv run --env-file .env python -m resourcey tasks list app:manifest``.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from resourcey.core.manifest import Manifest
from resourcey.tasks.scheduler import BackgroundTaskScheduler, scheduler_from_manifest
from resourcey.util.import_paths import resolve_import_path

if TYPE_CHECKING:
    import argparse

# The manifest spec is ``module:attr`` (uvicorn's convention); ``resolve_import_path``
# also accepts ``module.attr``, so both forms work.
_MANIFEST_HELP = "The app's manifest as 'module:manifest' (e.g. 'myapp.app:manifest')."


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tasks`` subcommand under the top-level CLI dispatcher."""
    tasks = subparsers.add_parser(
        "tasks",
        help="List or run an app's background tasks.",
        description=(
            "List an app's background tasks and their schedules, or run one (or all) "
            "once. The manifest is loaded by a 'module:manifest' spec."
        ),
    )
    tsub = tasks.add_subparsers(dest="tasks_command", required=True)

    listing = tsub.add_parser("list", help="Print each task with its schedule and enabled flag.")
    listing.add_argument("manifest", help=_MANIFEST_HELP)

    run = tsub.add_parser("run", help="Run one task by name, or all enabled tasks, once.")
    run.add_argument("manifest", help=_MANIFEST_HELP)
    run.add_argument(
        "name", nargs="?", default=None, help="Task name (default: all enabled tasks)."
    )


def load_manifest(spec: str) -> Manifest:
    """Resolve a ``module:manifest`` spec to a :class:`~resourcey.core.manifest.Manifest`."""
    resolved = resolve_import_path(spec)
    if not isinstance(resolved, Manifest):
        raise TypeError(
            f"Import path {spec!r} resolved to {type(resolved).__name__}, not a Manifest."
        )
    return resolved


def format_tasks(scheduler: BackgroundTaskScheduler) -> str:
    """A human-readable listing of each task: name, schedule, enabled."""
    lines: list[str] = []
    for task in scheduler.tasks:
        schedule = task.schedule or "(manual only)"
        state = "enabled" if task.enabled else "disabled"
        lines.append(f"{task.name}\t{schedule}\t{state}")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    """Execute the parsed ``tasks`` subcommand. Returns a process exit code."""
    manifest = load_manifest(args.manifest)
    scheduler = scheduler_from_manifest(manifest)

    if args.tasks_command == "list":
        print(format_tasks(scheduler))
        return 0

    # A one-shot run: enter the manifest's managers *except* the scheduler, so a
    # storage-backed task has a live engine but the periodic loop stays dormant.
    async with AsyncExitStack() as stack:
        for manager in manifest.managers:
            if manager is scheduler:
                continue
            await stack.enter_async_context(manager)
        await scheduler.run_once(args.name)
    return 0


def run(args: argparse.Namespace) -> int:
    """Synchronous wrapper around :func:`_run` for the CLI dispatcher."""
    import asyncio

    return asyncio.run(_run(args))
