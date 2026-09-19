"""CLI entry point: ``python -m resourcey`` / ``resourcey`` console script.

A minimal command dispatcher kept tiny on purpose — the migrations CLI (#3)
and future subcommands extend it. ``resourcey run`` (the default) launches
uvicorn against :func:`resourcey.app.create_app` (the factory, not a
pre-built app instance) so reload works, honouring ``RESOURCEY_HOST`` /
``RESOURCEY_PORT`` from config. ``--host`` / ``--port`` overrides win over
config.
"""

from __future__ import annotations

import sys

from resourcey.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
