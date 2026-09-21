"""The ``resourcey`` CLI — a tiny command dispatcher.

The ``resourcey migrate`` subcommand wraps Alembic for migration generation
and apply/rollback. Apps are started with ``uvicorn <app_module>:app`` (the
standard FastAPI path) — there is no ``resourcey run`` because the CLI cannot
know the user's app module.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any


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

    # Register the ``migrate`` subcommand into the dispatcher.
    from resourcey.migrate import migrate_cli

    migrate_cli.add_subparser(subparsers)
    return parser


# Each command maps to a callable taking the parsed Namespace.
_COMMANDS: dict[str, Callable[[Any], int]] = {}

# ``migrate`` delegates to the migrations CLI.
from resourcey.migrate import migrate_cli as _migrate_cli  # noqa: E402

_COMMANDS["migrate"] = _migrate_cli.run
