"""Tests for ``v2`` Alembic migration generation (issue #78).

``generate_migration`` enumerates the models of a declarative base,
autogenerates a revision whose ``upgrade()``/``downgrade()`` create and drop the
manifest's tables, and returns the path. The ``__main__`` block generates only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from resourcey.v2.core.dto import DTO
from resourcey.v2.sql.migration import (
    _build_alembic_config,
    generate_migration,
    main,
    resolve_base,
    sync_database_url,
)
from resourcey.v2.sql.resource import SqlResource, V2Base

_BASE_PATH = "migration_helper:MigrationBase"


class Widget(DTO):
    id: int
    label: str


class Gadget(DTO):
    id: int
    weight: float


def _columns(db_file: str, table: str) -> list[str]:
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        return [c["name"] for c in inspect(engine).get_columns(table)]
    finally:
        engine.dispose()


def _tables(db_file: str) -> list[str]:
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        return inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _isolated_v2base() -> Iterator[None]:
    """Snapshot and restore ``V2Base``'s metadata + class registry around each test.

    Generated models are process-global on the base, so a test that generates a
    ``widgets`` table must not leak it into another.
    """
    saved_tables = dict(V2Base.metadata.tables)
    saved_registry = dict(V2Base.registry._class_registry)
    V2Base.metadata.clear()
    V2Base.registry._class_registry.clear()
    yield
    V2Base.metadata.clear()
    V2Base.registry._class_registry.clear()
    for table in saved_tables.values():
        V2Base.metadata._add_table(table.name, table.schema, table)
    V2Base.registry._class_registry.update(saved_registry)


def _session_factory(url: str) -> Any:
    engine = create_async_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def test_asyncpg_url_is_normalised():
    assert sync_database_url("postgresql+asyncpg://u:p@h/d") == "postgresql+psycopg2://u:p@h/d"


def test_aiosqlite_url_is_normalised():
    assert sync_database_url("sqlite+aiosqlite:///./x.db") == "sqlite:///./x.db"


def test_sync_urls_pass_through_unchanged():
    assert sync_database_url("sqlite:///:memory:") == "sqlite:///:memory:"
    assert sync_database_url("mysql+pymysql://u:p@h/d") == "mysql+pymysql://u:p@h/d"


# ---------------------------------------------------------------------------
# Base resolution
# ---------------------------------------------------------------------------


def test_base_class_passes_through():
    assert resolve_base(V2Base) is V2Base


def test_base_string_resolves():
    from migration_helper import MigrationBase

    assert resolve_base(_BASE_PATH) is MigrationBase


def test_bare_string_raises():
    with pytest.raises(ValueError, match=r"package\.module:Base"):
        resolve_base("no_colon_here")


def test_non_base_path_raises():
    with pytest.raises(TypeError, match="declarative base"):
        resolve_base("resourcey.v2.core.dto:DTO")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_config_materialises_the_script_directory(tmp_path):
    config = _build_alembic_config(V2Base, "sqlite:///:memory:", tmp_path / "migs")
    directory = Path(config.get_main_option("script_location"))
    assert (directory / "env.py").is_file()
    assert (directory / "versions").is_dir()
    assert (directory / "script.py.mako").is_file()
    assert config.attributes["target_metadata"] is V2Base.metadata


def _generate_migration_round_trip(tmp_path: Path, url: str) -> None:
    db_file = url.replace("sqlite+aiosqlite:///", "")
    migrations_dir = tmp_path / "migrations"
    maker = _session_factory(url)
    SqlResource(Widget, session_factory=maker)
    SqlResource(Gadget, session_factory=maker)

    path = generate_migration(
        V2Base, database_url=url, message="initial", migrations_dir=migrations_dir
    )
    body = Path(path).read_text()
    assert "create_table" in body
    assert "'widgets'" in body
    assert "'gadgets'" in body

    config = _build_alembic_config(V2Base, url, migrations_dir)
    alembic_command.upgrade(config, "head")
    assert {"widgets", "gadgets"} <= set(_tables(db_file))
    assert _columns(db_file, "widgets") == ["id", "label"]

    alembic_command.downgrade(config, "base")
    assert "widgets" not in _tables(db_file)


def test_generate_migration_round_trip(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    _generate_migration_round_trip(tmp_path, url)


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------


def test_main_generates_only_and_prints_the_path(tmp_path, capsys):
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    SqlResource(Widget, session_factory=_session_factory(url))
    rc = main(
        [_BASE_PATH, "-m", "cli", "--database-url", url, "--migrations-dir", str(tmp_path / "migs")]
    )
    assert rc == 0
    # Alembic also logs to stdout; the last line is the revision path.
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    assert Path(printed).is_file()
    # It generated only: no revision was applied, so the manifest's tables do
    # not exist (Alembic may still create its own version bookkeeping table).
    assert "widgets" not in _tables(str(tmp_path / "cli.db"))


def test_main_falls_back_to_the_env_var(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'env.db'}"
    SqlResource(Widget, session_factory=_session_factory(url))
    monkeypatch.setenv("RESOURCEY_DATABASE_URL", url)
    rc = main([_BASE_PATH, "-m", "env", "--migrations-dir", str(tmp_path / "migs2")])
    assert rc == 0


def test_main_requires_a_database_url(monkeypatch):
    monkeypatch.delenv("RESOURCEY_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main([_BASE_PATH, "-m", "x"])


def test_script_path_handles_the_alembic_return_shapes():
    from types import SimpleNamespace

    from resourcey.v2.sql.migration import _script_path

    assert _script_path(None) is None
    assert _script_path([]) is None
    assert _script_path(SimpleNamespace(path="/x/rev.py")) == "/x/rev.py"
    assert _script_path([SimpleNamespace(path="/y/rev.py")]) == "/y/rev.py"
    assert _script_path(SimpleNamespace(path=None)) is None


def test_generate_migration_raises_when_alembic_returns_no_path(tmp_path, monkeypatch):
    from resourcey.v2.sql import migration as migration_module

    monkeypatch.setattr(migration_module.alembic_command, "revision", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="did not produce a revision file path"):
        migration_module.generate_migration(
            V2Base,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
            message="x",
            migrations_dir=tmp_path / "m",
        )
