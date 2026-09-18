"""Minimal ``.env`` file loader.

Reads a ``KEY=VALUE`` file into :data:`os.environ` using
:func:`os.environ.setdefault` so real environment variables win over file
values (documented precedence: env > ``.env``). No new dependency is
introduced; values are loaded verbatim — no shell interpolation / expansion
and therefore no shell-injection surface.

The loader is a no-op when the file is absent (the normal production case).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = ".env"
_ENV_FILE_VAR = "RESOURCEY_ENV_FILE"


def load_dotenv(path: str | Path | None = None) -> None:
    """Load a ``.env`` file into :data:`os.environ`.

    Args:
        path: File to load. When ``None`` the path is taken from the
            ``RESOURCEY_ENV_FILE`` env var, falling back to ``.env`` in the
            current working directory. A missing file is silently ignored.
    """
    if path is None:
        path = os.environ.get(_ENV_FILE_VAR, DEFAULT_ENV_FILE)
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        _load_line(line)


def _load_line(line: str) -> None:
    """Parse and apply a single ``KEY=VALUE`` line."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    key, sep, value = stripped.partition("=")
    if not sep:
        return
    key = key.strip()
    if not key:
        return
    value = _unquote(value.strip())
    # Real env vars win over file values.
    os.environ.setdefault(key, value)


def _unquote(value: str) -> str:
    """Strip one matching surrounding quote pair (``"``, ``'``, or backtick)."""
    if len(value) >= 2:
        first = value[0]
        if first in ('"', "'", "`") and value[-1] == first:
            return value[1:-1]
    return value
