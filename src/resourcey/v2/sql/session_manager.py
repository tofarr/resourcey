"""``SqlSessionManager`` — build and own the engines behind ``SqlConfig`` (issue #74).

The manager turns a :class:`~resourcey.v2.sql.sql_config.SqlConfig` into session
makers on demand: engines are built **lazily** on first use of a connection and
cached for the manager's life, and :meth:`__aexit__` disposes every engine it
built. Entering is an **idempotent** marker, so a fresh ``TestClient`` per test
case against one app works without an error; exiting clears the engine cache so
the next enter (or first use) rebuilds.

The manager hands out session *makers*, never sessions: opening a session stays
with the code that owns the transaction, so there is one storage-ownership story
(``v2/core``'s "whoever opens the storage owns its commit and close") rather
than two. It extends that rule to itself — using an un-entered manager raises,
so a caller who forgets ``async with manifest`` fails loudly instead of leaking
engines nobody disposes.

A module-level :func:`get_sql_session_manager` (configured from
:meth:`SqlConfig.get_instance`) is the global accessor ``SqlResource`` uses by
default; :func:`clear_sql_session_manager_cache` is the test-only reset,
mirroring ``BaseConfig.clear_instance_cache`` / ``Singleton.clear_singleton_cache``.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.sql.db_config import DbConfig
from resourcey.v2.sql.sql_config import SqlConfig


class SqlSessionManager:
    """Owns one async engine + session maker per configured SQL connection.

    Args:
        config: The named connections to serve. A lookup with no name uses the
            first; an unknown name or an empty list raises
            :class:`~resourcey.v2.core.errors.ResourceyConfigError`.
    """

    def __init__(self, config: SqlConfig) -> None:
        self._config = config
        self._engines: dict[str, AsyncEngine] = {}
        self._makers: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._entered = False

    # ------------------------------------------------------------------
    # Connection resolution
    # ------------------------------------------------------------------

    def _resolve(self, name: str | None) -> DbConfig:
        """The named connection, or the first when ``name`` is ``None``.

        An empty connection list and an unknown name are both misconfigurations
        and raise rather than falling back to a hidden default.
        """
        connections = self._config.sql_connections
        if not connections:
            raise ResourceyConfigError(
                "No SQL connections are configured; set at least one "
                "APP_SQL_CONNECTIONS_<n>_NAME / _URL (there is no built-in default)."
            )
        if name is None:
            return connections[0]
        for connection in connections:
            if connection.name == name:
                return connection
        known = ", ".join(repr(c.name) for c in connections)
        raise ResourceyConfigError(f"Unknown SQL connection name {name!r}; known names: {known}")

    async def get_session_maker(self, name: str | None = None) -> async_sessionmaker[AsyncSession]:
        """The session maker for connection ``name`` (default: the first).

        The engine is built and cached on first use. Raises
        :class:`~resourcey.v2.core.errors.ResourceyConfigError` when the manager
        has not been entered, when ``name`` is unknown, or when no connections
        are configured.
        """
        if not self._entered:
            raise ResourceyConfigError(
                "SqlSessionManager used outside its lifecycle; enter it (e.g. via "
                "the manifest's ``async with``) before requesting a session maker."
            )
        connection = self._resolve(name)
        maker = self._makers.get(connection.name)
        if maker is None:
            engine = create_async_engine(connection.database_url)
            self._engines[connection.name] = engine
            maker = async_sessionmaker(engine, expire_on_commit=False)
            self._makers[connection.name] = maker
        return maker

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SqlSessionManager:
        """Mark the manager usable; idempotent so re-entering is safe."""
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Dispose every engine built and clear the cache for a fresh rebuild."""
        engines, self._engines = self._engines, {}
        self._makers = {}
        self._entered = False
        for engine in engines.values():
            await engine.dispose()

    @property
    def entered(self) -> bool:
        """Whether the manager is currently inside its ``async with`` block."""
        return self._entered


_cached_manager: SqlSessionManager | None = None


def get_sql_session_manager() -> SqlSessionManager:
    """The process-wide manager, built from ``SqlConfig.get_instance()``.

    The instance is cached (engines are still built lazily per use); call
    :func:`clear_sql_session_manager_cache` to pick up a different config.
    """
    global _cached_manager
    if _cached_manager is None:
        _cached_manager = SqlSessionManager(SqlConfig.get_instance())
    return _cached_manager


def clear_sql_session_manager_cache() -> None:
    """Drop the cached process-wide manager so the next call rebuilds it.

    Test-only: engines are disposed by the manager's own ``__aexit__``, so this
    only forgets a manager built against a stale config.
    """
    global _cached_manager
    _cached_manager = None
