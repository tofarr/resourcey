"""Tests for the ``v2`` named SQL connections (issue #74).

Covers ``DbConfig`` (required name, password splicing), ``SqlConfig``
(``APP_SQL_CONNECTIONS_<n>_*`` parsing, duplicate/blank name rejection), and
``SqlSessionManager`` (default/named resolution, unknown name, empty config,
lazy engines, enter/exit disposal, idempotent enter, un-entered use).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from resourcey.v2.config.config_base import _reset_config_prefix
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.sql.db_config import DbConfig
from resourcey.v2.sql.session_manager import (
    SqlSessionManager,
    clear_sql_session_manager_cache,
    get_sql_session_manager,
)
from resourcey.v2.sql.sql_config import SqlConfig


@pytest.fixture(autouse=True)
def _clean() -> None:
    _reset_config_prefix()
    clear_sql_session_manager_cache()
    yield
    clear_sql_session_manager_cache()
    _reset_config_prefix()


# ---------------------------------------------------------------------------
# DbConfig
# ---------------------------------------------------------------------------


class TestDbConfig:
    def test_name_required(self):
        with pytest.raises(ValidationError):
            DbConfig()  # type: ignore[call-arg]

    def test_default_url(self):
        assert DbConfig(name="main").url == "sqlite+aiosqlite:///app.db"

    def test_database_url_without_password_is_verbatim(self):
        config = DbConfig(name="main", url="sqlite+aiosqlite:///x.db")
        assert config.database_url == "sqlite+aiosqlite:///x.db"

    def test_url_embedded_password_preserved(self):
        config = DbConfig(name="main", url="postgresql+asyncpg://u:secret@h/db")
        assert config.database_url == "postgresql+asyncpg://u:secret@h/db"

    def test_password_spliced_and_percent_encoded(self):
        config = DbConfig(
            name="main",
            url="postgresql+asyncpg://u@h/db",
            password="p@ss/w:rd",
        )
        # Reserved characters are percent-encoded by SQLAlchemy's URL parser.
        assert config.database_url.startswith("postgresql+asyncpg://u:")
        assert "p%40ss" in config.database_url
        assert config.database_url.endswith("@h/db")


# ---------------------------------------------------------------------------
# SqlConfig
# ---------------------------------------------------------------------------


class TestSqlConfig:
    def test_empty_by_default(self):
        assert SqlConfig().sql_connections == []

    def test_parses_sequential_env(self, monkeypatch):
        monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
        monkeypatch.setenv("APP_SQL_CONNECTIONS_0_URL", "sqlite+aiosqlite:///main.db")
        monkeypatch.setenv("APP_SQL_CONNECTIONS_1_NAME", "reports")
        monkeypatch.setenv("APP_SQL_CONNECTIONS_1_URL", "sqlite+aiosqlite:///reports.db")
        config = SqlConfig.get_instance()
        assert [c.name for c in config.sql_connections] == ["main", "reports"]
        assert config.sql_connections[1].url == "sqlite+aiosqlite:///reports.db"

    def test_duplicate_names_rejected(self, monkeypatch):
        monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "main")
        monkeypatch.setenv("APP_SQL_CONNECTIONS_1_NAME", "main")
        with pytest.raises(ResourceyConfigError, match="Duplicate"):
            SqlConfig.get_instance()

    def test_blank_name_rejected(self, monkeypatch):
        monkeypatch.setenv("APP_SQL_CONNECTIONS_0_NAME", "   ")
        with pytest.raises(ResourceyConfigError, match="must not be empty"):
            SqlConfig.get_instance()


# ---------------------------------------------------------------------------
# SqlSessionManager
# ---------------------------------------------------------------------------


def _config(*names: str) -> SqlConfig:
    return SqlConfig(
        sql_connections=[
            DbConfig(name=name, url=f"sqlite+aiosqlite:///{name}.db") for name in names
        ]
    )


def _record_disposals(monkeypatch: pytest.MonkeyPatch, disposed: list[object]) -> None:
    """Wrap ``AsyncEngine.dispose`` so a real disposal is observable."""
    original = AsyncEngine.dispose

    async def spy(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)
        await original(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)


class TestSqlSessionManager:
    async def test_un_entered_use_raises(self):
        manager = SqlSessionManager(_config("main"))
        with pytest.raises(ResourceyConfigError, match="outside its lifecycle"):
            await manager.get_session_maker()

    async def test_default_is_first_connection(self):
        manager = SqlSessionManager(_config("main", "reports"))
        async with manager:
            maker = await manager.get_session_maker()
            assert maker is await manager.get_session_maker("main")

    async def test_named_connection(self):
        manager = SqlSessionManager(_config("main", "reports"))
        async with manager:
            default = await manager.get_session_maker()
            reports = await manager.get_session_maker("reports")
            assert default is not reports

    async def test_unknown_name_raises(self):
        manager = SqlSessionManager(_config("main"))
        async with manager:
            with pytest.raises(ResourceyConfigError, match="Unknown SQL connection name"):
                await manager.get_session_maker("nope")

    async def test_lookup_is_case_sensitive(self):
        manager = SqlSessionManager(_config("Main"))
        async with manager:
            with pytest.raises(ResourceyConfigError):
                await manager.get_session_maker("main")

    async def test_empty_connections_raises(self):
        manager = SqlSessionManager(SqlConfig())
        async with manager:
            with pytest.raises(ResourceyConfigError, match="No SQL connections"):
                await manager.get_session_maker()

    async def test_engines_built_lazily_and_cached(self):
        manager = SqlSessionManager(_config("main"))
        async with manager:
            assert manager._engines == {}
            await manager.get_session_maker()
            assert "main" in manager._engines
            first = manager._engines["main"]
            await manager.get_session_maker()
            assert manager._engines["main"] is first

    async def test_exit_disposes_and_clears(self, monkeypatch):
        disposed: list[object] = []
        _record_disposals(monkeypatch, disposed)
        manager = SqlSessionManager(_config("main"))
        async with manager:
            await manager.get_session_maker()
            engine = manager._engines["main"]
            assert disposed == []
        assert manager._engines == {}
        assert disposed == [engine]

    async def test_reenter_rebuilds_after_exit(self):
        manager = SqlSessionManager(_config("main"))
        async with manager:
            await manager.get_session_maker()
            first = manager._engines["main"]
        async with manager:
            await manager.get_session_maker()
            second = manager._engines["main"]
            assert second is not first

    async def test_enter_is_idempotent(self):
        manager = SqlSessionManager(_config("main"))
        async with manager:
            await manager.get_session_maker()
            engine = manager._engines["main"]
            # Re-entering does not raise and does not rebuild.
            await manager.__aenter__()
            await manager.get_session_maker()
            assert manager._engines["main"] is engine


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------


class TestGlobalManager:
    def test_cached_across_calls(self):
        first = get_sql_session_manager()
        second = get_sql_session_manager()
        assert first is second

    def test_clear_rebuilds(self):
        first = get_sql_session_manager()
        clear_sql_session_manager_cache()
        assert get_sql_session_manager() is not first
