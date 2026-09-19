"""The ``resourcey`` CLI — a tiny command dispatcher.

``resourcey run`` (the default when invoked bare) starts uvicorn against
:func:`resourcey.app.create_app`. ``--host`` / ``--port`` override the
config-driven values; without them, ``RESOURCEY_HOST`` / ``RESOURCEY_PORT``
are honoured.

The dispatcher is intentionally minimal so the migrations CLI (#3) and
future subcommands can register themselves without reworking the plumbing.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a ``resourcey`` command. Returns a process exit code.

    With no subcommand, defaults to ``run`` (``resourcey`` with no args starts
    the server).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        # No subcommand given — synthesize the run namespace (argparse leaves
        # a bare Namespace without run's host/port/reload defaults).
        args = argparse.Namespace(command="run", host=None, port=None, reload=False)
    return int(_COMMANDS[args.command](args) or 0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resourcey", description="resourcey CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Start the uvicorn app server (default).")
    run.add_argument("--host", default=None, help="Override RESOURCEY_HOST.")
    run.add_argument("--port", type=int, default=None, help="Override RESOURCEY_PORT.")
    run.add_argument("--reload", action="store_true", help="Enable uvicorn reload.")

    # Register the ``migrate`` subcommand (issue #3) into the dispatcher.
    from resourcey.migrate import migrate_cli

    migrate_cli.add_subparser(subparsers)
    return parser


# Each command maps to a callable taking the parsed Namespace.
_COMMANDS: dict[str, Callable[[Any], int]] = {}


def run(args: Any) -> int:
    """``resourcey run`` — start uvicorn against the app factory.

    ``--host`` / ``--port`` win over config; otherwise config values are used.
    Launches uvicorn against ``resourcey.app:create_app`` (the factory) so
    reload works when ``--reload`` is set.
    """
    import uvicorn

    config = get_config_as(FrameworkConfig)
    host = args.host or config.host
    port = args.port if args.port is not None else config.port
    uvicorn.run(
        "resourcey.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=bool(args.reload),
    )
    return 0


# ``run`` is the default command and is registered under its name too.
_COMMANDS["run"] = run

# ``migrate`` delegates to the migrations CLI (issue #3).
from resourcey.migrate import migrate_cli as _migrate_cli  # noqa: E402

_COMMANDS["migrate"] = _migrate_cli.run
