"""``python -m resourcey`` — the framework command dispatcher (issue #15).

Apps are started with ``uvicorn <app_module>:app`` (the standard FastAPI path) —
there is no ``resourcey run``, because the CLI cannot know the user's app module.
What the dispatcher does provide is the background-task entry point::

    python -m resourcey tasks list app:manifest
    python -m resourcey tasks run  app:manifest [name]

The framework does no ``.env`` loading of its own, so run it with the app's
environment populated (e.g. ``uv run --env-file .env python -m resourcey …``).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

# Each subcommand maps to a callable taking the parsed Namespace and returning an
# exit code. Populated lazily by ``_build_parser`` so importing the dispatcher
# does not import every command's dependencies.
_COMMANDS: dict[str, Callable[[Any], int]] = {}


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a ``resourcey`` command. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return int(_COMMANDS[args.command](args) or 0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resourcey", description="resourcey CLI.")
    subparsers = parser.add_subparsers(dest="command")

    from resourcey.tasks import cli as tasks_cli

    tasks_cli.add_subparser(subparsers)
    _COMMANDS["tasks"] = tasks_cli.run
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
