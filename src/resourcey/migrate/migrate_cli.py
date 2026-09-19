"""``resourcey ...`` CLI (issue #3).

The ``resourcey`` entry point currently fronts the ``migrate`` subcommand,
which wraps Alembic via :mod:`resourcey.migrate.migrate_runner`:

* ``resourcey migrate init``                  — materialise ``env.py`` + ``versions/``.
* ``resourcey migrate autogenerate -m "..."`` — autogenerate a draft revision
  (``generate`` is accepted as an alias).
* ``resourcey migrate upgrade [rev]``         — apply migrations (default ``head``).
* ``resourcey migrate downgrade <rev>``       — roll back (``-1`` or a revision id).

It resolves the active config
(:func:`resourcey.config.config_runtime.get_config`) for the database URL and
migration settings. Generated revisions are drafts — review them before
applying (see the ``migrations`` skill and the README for the
rename-as-drop-create caveat). Run ``alembic`` directly to escape the wrapper.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import cast

from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config
from resourcey.migrate import migrate_runner


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``resourcey`` top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="resourcey",
        description="resourcey framework CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser(
        "migrate",
        help="Alembic migration generation and apply/rollback.",
        description=(
            "Alembic migration generation and apply/rollback for resourcey. "
            "Generated revisions are review-required drafts."
        ),
    )
    msub = migrate.add_subparsers(dest="migrate_command", required=True)

    msub.add_parser("init", help="Materialise env.py and versions/ in the migrations directory.")

    gen = msub.add_parser(
        "autogenerate",
        aliases=["generate"],
        help="Autogenerate a draft Alembic revision from resource models.",
    )
    gen.add_argument("-m", "--message", required=True, help="Revision message.")

    up = msub.add_parser("upgrade", help="Apply migrations (default: head).")
    up.add_argument("revision", nargs="?", default="head", help="Target revision (default: head).")

    down = msub.add_parser("downgrade", help="Roll back migrations.")
    down.add_argument("revision", help="Target revision (e.g. -1 or a revision id).")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``resourcey``. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "migrate":  # pragma: no cover — argparse enforces a subcommand
        parser.error(f"Unknown command: {args.command}")

    config = get_config()
    # ``get_config`` is typed as ``BaseConfig`` (it may return an app subclass),
    # but the migrate commands read ``database`` / ``migrations`` which are
    # ``FrameworkConfig`` attributes. ``cast`` narrows: the default path and any
    # app config extending ``FrameworkConfig`` satisfy this; a non-framework
    # config would raise at runtime on attribute access anyway.
    framework_config = cast(FrameworkConfig, config)
    database_url = framework_config.database.database_url
    migration_config = framework_config.migrations
    resource_modules = framework_config.resource_modules

    cmd = args.migrate_command
    if cmd == "init":
        directory = migrate_runner.init(
            migration_config,
            database_url=database_url,
            resource_modules=resource_modules,
        )
        print(f"Initialised migrations in {directory}")
    elif cmd == "autogenerate":
        path = migrate_runner.generate(
            migration_config,
            database_url=database_url,
            message=args.message,
            resource_modules=resource_modules,
        )
        print(
            f"Generated draft revision: {path}\n"
            "Review it before applying (renames look like drop+create)."
        )
    elif cmd == "upgrade":
        migrate_runner.upgrade(
            migration_config,
            database_url=database_url,
            revision=args.revision,
            resource_modules=resource_modules,
        )
        print(f"Upgraded to {args.revision}")
    elif cmd == "downgrade":
        migrate_runner.downgrade(
            migration_config,
            database_url=database_url,
            revision=args.revision,
            resource_modules=resource_modules,
        )
        print(f"Downgraded to {args.revision}")
    else:  # pragma: no cover — argparse enforces a subcommand
        parser.error(f"Unknown migrate command: {cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
