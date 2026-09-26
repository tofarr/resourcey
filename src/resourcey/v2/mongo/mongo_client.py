"""``MongoClientManager`` — build and own the clients behind ``MongoConfig`` (issue #80).

The manager turns a :class:`~resourcey.v2.mongo.mongo_config.MongoConfig` into
``(client, database_name)`` pairs on demand: a client is built **lazily** on
first use of a connection and cached for the manager's life, and
:meth:`__aexit__` closes every client it built. Entering is an **idempotent**
marker, so a fresh ``TestClient`` per test case against one app works without an
error; exiting clears the client cache so the next enter (or first use)
rebuilds.

It is the :class:`~resourcey.v2.sql.session_manager.SqlSessionManager` analogue
and extends the same storage-ownership story: a resource resolves its client
through the manager, the manager owns the client's lifetime, and using an
un-entered manager raises so a caller who forgets ``async with manifest`` fails
loudly instead of leaking clients nobody closes.

An ``embedded://<db>`` (or bare ``embedded``) URL builds the in-process
:class:`~resourcey.v2.mongo.embedded.AsyncEmbeddedClient`; any other URL builds a
real ``motor`` client, and a missing ``motor`` raises an actionable
:class:`ImportError` naming the ``resourcey[mongodb]`` extra rather than an
opaque ``ModuleNotFoundError``.

A module-level :func:`get_mongo_client_manager` (configured from
:meth:`MongoConfig.get_instance`) is the global accessor ``MongoResource`` uses
by default; :func:`clear_mongo_client_manager_cache` is the test-only reset,
mirroring the SQL manager and ``BaseConfig.clear_instance_cache``.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from typing import Any

from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.mongo.mongo_config import MongoConfig, MongoConnectionConfig

# Database name used when the connection URL names no database (a bare
# ``embedded`` marker or a ``mongodb://`` URL with no path).
DEFAULT_MONGO_DATABASE = "resourcey"


def _require_motor() -> Any:
    """Import ``motor`` or fail with an actionable message naming the extra."""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "A non-embedded Mongo connection requires the 'mongodb' extra. Install it "
            "with `pip install resourcey[mongodb]` or `uv sync --extra mongodb`."
        ) from exc
    return AsyncIOMotorClient


def _build_embedded_client() -> Any:
    """Build the in-process ``mongomock`` client (imported lazily).

    ``mongomock`` is a dev-only dependency (the embedded path is for tests and
    local development), so importing it here keeps ``v2/mongo`` import-safe when
    only the ``mongodb`` extra is installed.
    """
    from resourcey.v2.mongo.embedded import AsyncEmbeddedClient

    return AsyncEmbeddedClient()


class MongoClientManager:
    """Owns one client per configured Mongo connection.

    Args:
        config: The named connections to serve. A lookup with no name uses the
            first; an unknown name or an empty list raises
            :class:`~resourcey.v2.core.errors.ResourceyConfigError`.
    """

    def __init__(self, config: MongoConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}
        self._entered = False

    # ------------------------------------------------------------------
    # Connection resolution
    # ------------------------------------------------------------------

    def _resolve(self, name: str | None) -> MongoConnectionConfig:
        """The named connection, or the first when ``name`` is ``None``.

        An empty connection list and an unknown name are both misconfigurations
        and raise rather than falling back to a hidden default.
        """
        connections = self._config.connections
        if not connections:
            raise ResourceyConfigError(
                "No Mongo connections are configured; set at least one "
                "APP_MONGO_CONNECTIONS_<n>_NAME / _URL (there is no built-in default)."
            )
        if name is None:
            return connections[0]
        for connection in connections:
            if connection.name == name:
                return connection
        known = ", ".join(repr(c.name) for c in connections)
        raise ResourceyConfigError(f"Unknown Mongo connection name {name!r}; known names: {known}")

    async def get_client(self, name: str | None = None) -> tuple[Any, str]:
        """The ``(client, database_name)`` for connection ``name`` (default: the first).

        The client is built and cached on first use. Raises
        :class:`~resourcey.v2.core.errors.ResourceyConfigError` when the manager
        has not been entered, when ``name`` is unknown, or when no connections
        are configured.
        """
        if not self._entered:
            raise ResourceyConfigError(
                "MongoClientManager used outside its lifecycle; enter it (e.g. via "
                "the manifest's ``async with``) before requesting a client."
            )
        connection = self._resolve(name)
        database_name = connection.mongo_database_name(DEFAULT_MONGO_DATABASE)
        client = self._clients.get(connection.name)
        if client is None:
            client = _build_client(connection)
            self._clients[connection.name] = client
        return client, database_name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> MongoClientManager:
        """Mark the manager usable; idempotent so re-entering is safe."""
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close every client built and clear the cache for a fresh rebuild."""
        clients, self._clients = self._clients, {}
        self._entered = False
        for client in clients.values():
            client.close()

    @property
    def entered(self) -> bool:
        """Whether the manager is currently inside its ``async with`` block."""
        return self._entered


def _build_client(connection: MongoConnectionConfig) -> Any:
    """Build the client for ``connection`` (embedded or a real ``motor`` client).

    An embedded URL builds the in-process :class:`AsyncEmbeddedClient`. A real
    URL builds a ``motor`` client, passing ``password`` **only when set** —
    ``password=None`` is not neutral to pymongo, it clears a URL-embedded
    password.
    """
    if connection.is_embedded:
        return _build_embedded_client()
    client_type = _require_motor()
    password = connection.mongo_password
    if password is not None:
        return client_type(connection.url, password=password)
    return client_type(connection.url)


_cached_manager: MongoClientManager | None = None


def get_mongo_client_manager() -> MongoClientManager:
    """The process-wide manager, built from ``MongoConfig.get_instance()``.

    The instance is cached (clients are still built lazily per use); call
    :func:`clear_mongo_client_manager_cache` to pick up a different config.
    """
    global _cached_manager
    if _cached_manager is None:
        _cached_manager = MongoClientManager(MongoConfig.get_instance())
    return _cached_manager


def clear_mongo_client_manager_cache() -> None:
    """Drop the cached process-wide manager so the next call rebuilds it.

    Test-only: clients are closed by the manager's own ``__aexit__``, so this
    only forgets a manager built against a stale config.
    """
    global _cached_manager
    _cached_manager = None
