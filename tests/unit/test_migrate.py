"""Tests for Alembic migration generation (issue #3).

Covers ``MigrationConfig`` defaults/env parsing, ``build_alembic_config``
(env.py + script.py.mako + versions/ materialisation, sync-URL conversion),
the manifest-driven env.py (imports ``RESOURCEY_MANIFEST`` and materialises
tables), the end-to-end generate -> upgrade -> downgrade round trip,
add-column autogeneration + rollback, ``init``, the CLI
(``resourcey migrate ...`` wired into the top-level dispatcher), and the
env-var-driven config path.

Uses a real on-disk SQLite database (Alembic drives a sync engine and must own
the connection lifecycle, so the in-memory savepoint isolation from
``conftest.py`` does not apply here). Each test gets its own temp directory and
database file. ``ResourceyBase.metadata`` and the class registry are
snapshotted and restored per test so generated tables/classes do not leak into
other test modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from resourcey.cli import main as cli_main
from resourcey.config.config_framework import FrameworkConfig, MigrationConfig
from resourcey.config.config_runtime import clear_config_cache, set_config
from resourcey.migrate import migrate_runner
from resourcey.resource.base import BaseResource
from resourcey.resource.sql import ResourceyBase

_MANIFEST_PATH = "migrate_manifest:manifest"


@pytest.fixture
def isolated_db(tmp_path: Path) -> tuple[str, str]:
    """Yield (db_url, db_file_path) for a fresh on-disk SQLite database."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    return db_url, str(db_file)


@pytest.fixture(autouse=True)
def _reset_metadata_and_env(monkeypatch):
    """Isolate ``ResourceyBase.metadata`` + class registry.

    Save/restore rather than clear: the metadata and class registry are
    process-global, and other test modules' resources stay registered in them.
    """
    saved_tables = dict(ResourceyBase.metadata.tables)
    saved_registry = dict(ResourceyBase.registry._class_registry)
    saved_caches: dict[type, dict[str, object]] = {}
    for cls in _all_subclasses(BaseResource):
        saved_caches[cls] = {
            attr: cls.__dict__[attr]
            for attr in ("_sqlalchemy_model", "_id_field")
            if attr in cls.__dict__
        }
        for attr in ("_sqlalchemy_model", "_id_field"):
            if attr in cls.__dict__:
                delattr(cls, attr)
    ResourceyBase.metadata.clear()
    ResourceyBase.registry._class_registry.clear()
    yield
    ResourceyBase.metadata.clear()
    ResourceyBase.registry._class_registry.clear()
    for table in saved_tables.values():
        ResourceyBase.metadata._add_table(table.name, table.schema, table)
    ResourceyBase.registry._class_registry.update(saved_registry)
    for cls, cache in saved_caches.items():
        for attr in ("_sqlalchemy_model", "_id_field"):
            if attr in cache:
                setattr(cls, attr, cache[attr])
            elif attr in cls.__dict__:
                delattr(cls, attr)
    clear_config_cache()


def _all_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_all_subclasses(sub))
    return found


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


def _set_manifest_env(monkeypatch: pytest.MonkeyPatch, path: str = _MANIFEST_PATH) -> None:
    """Set RESOURCEY_MANIFEST (the env.py convention)."""
    monkeypatch.setenv("RESOURCEY_MANIFEST", path)


def _config(tmp_path: Path) -> tuple[FrameworkConfig, str]:
    """Build a FrameworkConfig pointing at a temp migrations dir + db."""
    migrations_dir = str(tmp_path / "migrations")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    cfg = FrameworkConfig(
        migrations=MigrationConfig(migrations_dir=migrations_dir),
        manifest=_MANIFEST_PATH,
    )
    return cfg, db_url


# ---------------------------------------------------------------------------
# MigrationConfig
# ---------------------------------------------------------------------------


class TestMigrationConfig:
    def test_defaults(self):
        cfg = MigrationConfig()
        assert cfg.migrations_dir == "migrations"

    def test_framework_config_has_migrations(self):
        cfg = FrameworkConfig()
        assert cfg.migrations.migrations_dir == "migrations"

    def test_env_overrides_migrations_dir(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_MIGRATIONS_MIGRATIONS_DIR", "/tmp/envmigs")
        FrameworkConfig.clear_instance_cache()
        try:
            cfg = FrameworkConfig.get_instance()
            assert cfg.migrations.migrations_dir == "/tmp/envmigs"
        finally:
            FrameworkConfig.clear_instance_cache()


# ---------------------------------------------------------------------------
# _sync_database_url
# ---------------------------------------------------------------------------


class TestSyncDatabaseUrl:
    def test_asyncpg_to_psycopg2(self):
        assert (
            migrate_runner._sync_database_url("postgresql+asyncpg://u:p@h/d")
            == "postgresql+psycopg2://u:p@h/d"
        )

    def test_aiosqlite_to_sqlite(self):
        assert (
            migrate_runner._sync_database_url("sqlite+aiosqlite:///./test.db")
            == "sqlite:///./test.db"
        )

    def test_unknown_driver_passthrough(self):
        assert (
            migrate_runner._sync_database_url("mysql+pymysql://u:p@h/d")
            == "mysql+pymysql://u:p@h/d"
        )

    def test_already_sync_passthrough(self):
        assert migrate_runner._sync_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


# ---------------------------------------------------------------------------
# build_alembic_config
# ---------------------------------------------------------------------------


class TestBuildAlembicConfig:
    def test_materialises_env_versions_and_mako(self, tmp_path):
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        config = migrate_runner.build_alembic_config(cfg, database_url="sqlite:///./x.db")
        d = Path(config.get_main_option("script_location"))
        assert (d / "env.py").is_file()
        assert (d / "versions").is_dir()
        assert (d / "script.py.mako").is_file()
        assert config.get_main_option("sqlalchemy.url") == "sqlite:///./x.db"

    def test_env_py_source_targets_resourcey_metadata(self):
        assert "ResourceyBase" in migrate_runner.ENV_PY_SOURCE
        assert "manifest" in migrate_runner.ENV_PY_SOURCE
        assert "materialize" in migrate_runner.ENV_PY_SOURCE

    def test_existing_env_not_overwritten(self, tmp_path):
        d = tmp_path / "migs"
        d.mkdir()
        (d / "env.py").write_text("# custom", encoding="utf-8")
        cfg = MigrationConfig(migrations_dir=str(d))
        migrate_runner.build_alembic_config(cfg, database_url="sqlite:///./x.db")
        assert (d / "env.py").read_text() == "# custom"

    def test_explicit_migrations_dir_overrides_config(self, tmp_path):
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "cfg_dir"))
        config = migrate_runner.build_alembic_config(
            cfg, database_url="sqlite:///./x.db", migrations_dir=str(tmp_path / "override")
        )
        assert config.get_main_option("script_location") == str(tmp_path / "override")


# ---------------------------------------------------------------------------
# End-to-end round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_generate_upgrade_downgrade(self, tmp_path, isolated_db, monkeypatch):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path)
        _set_manifest_env(monkeypatch)
        set_config(cfg)
        try:
            path = migrate_runner.generate(cfg.migrations, database_url=db_url, message="init")
            assert Path(path).is_file()
            assert "create_table" in Path(path).read_text()

            migrate_runner.upgrade(cfg.migrations, database_url=db_url)
            assert "widgets" in _tables(db_file)
            assert _columns(db_file, "widgets") == ["id", "label", "created_at"]

            migrate_runner.downgrade(cfg.migrations, database_url=db_url, revision="base")
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()

    def test_upgrade_head_then_downgrade_one(self, tmp_path, isolated_db, monkeypatch):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path)
        _set_manifest_env(monkeypatch)
        set_config(cfg)
        try:
            migrate_runner.generate(cfg.migrations, database_url=db_url, message="first")
            migrate_runner.upgrade(cfg.migrations, database_url=db_url)
            assert "widgets" in _tables(db_file)

            migrate_runner.downgrade(cfg.migrations, database_url=db_url, revision="-1")
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()


class TestAddColumn:
    def test_generate_add_column_and_rollback(self, tmp_path, isolated_db, monkeypatch):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path)
        _set_manifest_env(monkeypatch)
        set_config(cfg)
        try:
            migrate_runner.generate(cfg.migrations, database_url=db_url, message="v1")
            migrate_runner.upgrade(cfg.migrations, database_url=db_url)
            assert "weight" not in _columns(db_file, "widgets")

            from sqlalchemy import Column, Float

            table = ResourceyBase.metadata.tables["widgets"]
            table.append_column(Column("weight", Float, nullable=True))

            p2 = migrate_runner.generate(cfg.migrations, database_url=db_url, message="add weight")
            text2 = Path(p2).read_text()
            assert "add_column" in text2
            assert "weight" in text2

            migrate_runner.upgrade(cfg.migrations, database_url=db_url)
            assert "weight" in _columns(db_file, "widgets")

            migrate_runner.downgrade(cfg.migrations, database_url=db_url, revision="-1")
            assert "weight" not in _columns(db_file, "widgets")
        finally:
            clear_config_cache()


class TestInit:
    def test_init_creates_files(self, tmp_path):
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        directory = migrate_runner.init(cfg, database_url="sqlite:///./x.db")
        d = Path(directory)
        assert (d / "env.py").is_file()
        assert (d / "versions").is_dir()
        assert (d / "script.py.mako").is_file()


class TestScriptPath:
    def test_none_returns_none(self):
        assert migrate_runner._script_path(None) is None

    def test_list_returns_none_when_empty(self):
        assert migrate_runner._script_path([]) is None

    def test_list_returns_first(self, tmp_path):
        class Fake:
            path = str(tmp_path / "rev.py")

        assert migrate_runner._script_path([Fake()]) == str(tmp_path / "rev.py")

    def test_object_without_path_returns_none(self):
        class Fake:
            pass

        assert migrate_runner._script_path(Fake()) is None

    def test_generate_with_no_manifest_returns_pass_revision(
        self, tmp_path, isolated_db, monkeypatch
    ):
        # No manifest -> metadata empty -> revision body is ``pass``.
        monkeypatch.delenv("RESOURCEY_MANIFEST", raising=False)
        db_url, _ = isolated_db
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        # Set a config with no manifest so env.py finds nothing to materialise.
        fw_cfg = FrameworkConfig(migrations=cfg, manifest="")
        set_config(fw_cfg)
        try:
            path = migrate_runner.generate(cfg, database_url=db_url, message="empty")
            body = Path(path).read_text()
            assert "pass" in body
        finally:
            clear_config_cache()


class TestCli:
    def test_migrate_requires_subcommand(self):
        with pytest.raises(SystemExit):
            cli_main(["migrate"])

    def test_autogenerate_requires_message(self):
        with pytest.raises(SystemExit):
            cli_main(["migrate", "autogenerate"])

    def test_generate_alias_accepted(self, tmp_path, monkeypatch):
        cfg = FrameworkConfig(
            migrations=MigrationConfig(migrations_dir=str(tmp_path / "migs")),
        )
        set_config(cfg)
        monkeypatch.setattr(
            type(cfg.database), "database_url", property(lambda self: "sqlite:///./x.db")
        )
        called: dict[str, str] = {}

        def _fake_generate(migration_config, *, database_url, message):
            called["message"] = message
            return str(tmp_path / "rev.py")

        monkeypatch.setattr(migrate_runner, "generate", _fake_generate)
        try:
            rc = cli_main(["migrate", "generate", "-m", "x"])
            assert rc == 0
            assert called["message"] == "x"
        finally:
            clear_config_cache()

    def test_init_via_cli(self, tmp_path, monkeypatch):
        cfg = FrameworkConfig(
            migrations=MigrationConfig(migrations_dir=str(tmp_path / "migs")),
        )
        set_config(cfg)
        monkeypatch.setattr("resourcey.migrate.migrate_cli.get_config", lambda: cfg)
        try:
            rc = cli_main(["migrate", "init"])
            assert rc == 0
            assert (tmp_path / "migs" / "env.py").is_file()
        finally:
            clear_config_cache()

    def test_full_cli_round_trip(self, tmp_path, isolated_db, monkeypatch):
        db_url, db_file = isolated_db
        cfg = FrameworkConfig(
            migrations=MigrationConfig(migrations_dir=str(tmp_path / "migs")),
            manifest=_MANIFEST_PATH,
        )
        _set_manifest_env(monkeypatch)
        monkeypatch.setattr(type(cfg.database), "database_url", property(lambda self: db_url))
        set_config(cfg)
        try:
            assert cli_main(["migrate", "autogenerate", "-m", "init"]) == 0
            assert cli_main(["migrate", "upgrade"]) == 0
            assert "widgets" in _tables(db_file)
            assert cli_main(["migrate", "downgrade", "base"]) == 0
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()


class TestEnvDrivenConfig:
    def test_manifest_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RESOURCEY_MIGRATIONS_MIGRATIONS_DIR", str(tmp_path / "envmigs"))
        _set_manifest_env(monkeypatch)
        FrameworkConfig.clear_instance_cache()
        try:
            cfg = FrameworkConfig.get_instance()
            assert cfg.migrations.migrations_dir == str(tmp_path / "envmigs")
            assert cfg.manifest == _MANIFEST_PATH
        finally:
            FrameworkConfig.clear_instance_cache()
