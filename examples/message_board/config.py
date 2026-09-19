"""Example app config — SQLite by default.

``MessageBoardConfig`` subclasses :class:`FrameworkConfig` so the example boots
against a local SQLite database (file or in-memory) without requiring a running
Postgres. The default driver is ``sqlite+aiosqlite``; override via the standard
``RESOURCEY_DATABASE_*`` env vars (e.g. point them at Postgres for production).

The assembled URL is stored in ``database_url`` so the app, the migrations CLI,
and any escape-hatch engine all read one source of truth.
"""

from __future__ import annotations

from resourcey.config.config_framework import DbConfig, FrameworkConfig


class SqliteDbConfig(DbConfig):
    """Database config defaulting to a local SQLite file.

    ``protocol`` defaults to ``sqlite+aiosqlite`` (async driver, already a dev
    dependency). ``db_name`` is the SQLite file path relative to the working
    directory (``message_board.db``). The standard ``host``/``port``/
    ``username``/``password`` fields are unused by SQLite but kept so the env
    template and the parent ``DbConfig`` shape stay consistent.
    """

    protocol: str = "sqlite+aiosqlite"
    db_name: str = "message_board.db"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""

    @property
    def database_url(self) -> str:
        """Assemble the async SQLite URL.

        SQLite ignores host/port/credentials, so the URL is simply
        ``{protocol}:///{db_name}``. A relative ``db_name`` resolves against the
        process working directory; an absolute path is honoured verbatim. Use
        ``:memory:`` for an ephemeral database (note: in-memory SQLite is
        connection-scoped, so a single shared connection pool is required).
        """
        return f"{self.protocol}:///{self.db_name}"


class MessageBoardConfig(FrameworkConfig):
    """Framework config for the message-board example (prefix ``RESOURCEY``).

    Overrides the database config to default to SQLite and the app server to
    bind on all interfaces (``0.0.0.0``) at port 8081, so the example does not
    collide with a framework dev server on the stock ``127.0.0.1:8000``. All
    other behaviour (CORS, resources, migrations) is inherited from
    :class:`FrameworkConfig` and configured via env vars or the ``.env`` file.
    """

    database: SqliteDbConfig = SqliteDbConfig()
    host: str = "0.0.0.0"
    port: int = 8081
