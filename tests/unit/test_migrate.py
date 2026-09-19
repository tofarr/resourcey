"""Tests for Alembic migration generation (issue #3).

Covers ``MigrationConfig`` defaults/env parsing, the resource registry
(``register_resource`` / ``get_registered_resources``), ``build_alembic_config``
(env.py + script.py.mako + versions/ materialisation, sync-URL conversion,
resource-module env propagation), ``import_resource_modules`` (CSV + sequential
indices + unimportable error), the end-to-end generate → upgrade → downgrade
round trip, add-column autogeneration + rollback, ``init``, the CLI
(``resourcey migrate ...``), and the env-var-driven config path.

Uses a real on-disk SQLite database (Alembic drives a sync engine and must own
the connection lifecycle, so the in-memory savepoint isolation from
``conftest.py`` does not apply here). Each test gets its own temp directory and
database file. ``ResourceyBase.metadata``, the class registry, and the resource
registry are snapshotted and restored per test so generated tables/classes do
not leak into other test modules.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from resourcey.config.config_framework import FrameworkConfig, MigrationConfig
from resourcey.config.config_runtime import clear_config_cache, set_config
from resourcey.migrate import migrate_cli, migrate_runner
from resourcey.resource.base import BaseResource, ResourceyBase
from resourcey.resource.registry import (
    clear_registry,
    get_registered_resources,
    register_resource,
)

_MODULE_A = "migrate_resources_a"
_MODULE_B = "migrate_resources_b"
_MODULES_ENV = "RESOURCEY_RESOURCE_MODULES"


@pytest.fixture
def isolated_db(tmp_path: Path) -> tuple[str, str]:
    """Yield (db_url, db_file_path) for a fresh on-disk SQLite database."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    return db_url, str(db_file)


@pytest.fixture(autouse=True)
def _reset_metadata_and_env(monkeypatch):
    """Isolate ``ResourceyBase.metadata`` + class registry + resource registry.

    Save/restore rather than clear: the metadata and class registry are
    process-global, and other test modules' resources stay registered in them.
    Clearing would break those modules when they run after this one. Instead we
    snapshot the tables, registry entries, and the resource registry, let each
    test rebuild from its own resource modules, and restore the snapshot on
    teardown so no test leaks generated tables/classes into the global state.
    """
    monkeypatch.delenv(_MODULES_ENV, raising=False)
    i = 0
    while True:
        key = f"{_MODULES_ENV}_{i}"
        if key not in os.environ:
            break
        monkeypatch.delenv(key, raising=False)
        i += 1

    saved_tables = dict(ResourceyBase.metadata.tables)
    saved_registry = dict(ResourceyBase.registry._class_registry)
    saved_resource_registry = get_registered_resources()
    saved_caches: dict[type, dict[str, object]] = {}
    for cls in _all_subclasses(BaseResource):
        saved_caches[cls] = {
            attr: cls.__dict__[attr]
            for attr in ("_sqlalchemy_model", "_id_field")
            if attr in cls.__dict__
        }
        # Drop the cached model so the next get_sql_alchemy_model() rebuilds
        # the table into the freshly-cleared metadata. Without this the cached
        # model still references the old (now-cleared) Table.
        for attr in ("_sqlalchemy_model", "_id_field"):
            if attr in cls.__dict__:
                delattr(cls, attr)
    ResourceyBase.metadata.clear()
    ResourceyBase.registry._class_registry.clear()
    clear_registry()
    yield
    # Restore: drop test-generated tables/classes, then re-register the snapshot.
    ResourceyBase.metadata.clear()
    ResourceyBase.registry._class_registry.clear()
    clear_registry()
    for table in saved_tables.values():
        ResourceyBase.metadata._add_table(table.name, table.schema, table)
    ResourceyBase.registry._class_registry.update(saved_registry)
    for cls in saved_resource_registry:
        register_resource(cls)
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


def _config(tmp_path: Path, modules: list[str]) -> tuple[FrameworkConfig, str]:
    """Build a FrameworkConfig pointing at a temp migrations dir."""
    migrations_dir = str(tmp_path / "migrations")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    cfg = FrameworkConfig(
        migrations=MigrationConfig(migrations_dir=migrations_dir),
        resource_modules=modules,
    )
    return cfg, db_url


class TestMigrationConfig:
    def test_defaults(self):
        cfg = MigrationConfig()
        assert cfg.migrations_dir == "migrations"

    def test_framework_config_has_migrations_and_resource_modules(self):
        cfg = FrameworkConfig()
        assert isinstance(cfg.migrations, MigrationConfig)
        assert cfg.migrations.migrations_dir == "migrations"
        assert cfg.resource_modules == []

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_MIGRATIONS_MIGRATIONS_DIR", "/tmp/custom_migrations")
        monkeypatch.setenv("RESOURCEY_RESOURCE_MODULES", '["a.b", "c.d"]')
        FrameworkConfig.clear_instance_cache()
        try:
            cfg = FrameworkConfig.get_instance()
            assert cfg.migrations.migrations_dir == "/tmp/custom_migrations"
            assert cfg.resource_modules == ["a.b", "c.d"]
        finally:
            FrameworkConfig.clear_instance_cache()

    def test_resource_modules_sequential(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_RESOURCE_MODULES", raising=False)
        monkeypatch.setenv("RESOURCEY_RESOURCE_MODULES_0", "a.b")
        monkeypatch.setenv("RESOURCEY_RESOURCE_MODULES_1", "c.d")
        FrameworkConfig.clear_instance_cache()
        try:
            cfg = FrameworkConfig.get_instance()
            assert cfg.resource_modules == ["a.b", "c.d"]
        finally:
            FrameworkConfig.clear_instance_cache()


class TestResourceRegistry:
    def test_register_materialises_model_and_records(self):
        class Gadget(BaseResource):
            id: int
            name: str

        register_resource(Gadget)
        assert Gadget in get_registered_resources()
        assert "gadgets" in ResourceyBase.metadata.tables

    def test_register_returns_class(self):
        class Gizmo(BaseResource):
            id: int

        result = register_resource(Gizmo)
        assert result is Gizmo

    def test_register_is_idempotent(self):
        class Doohickey(BaseResource):
            id: int

        register_resource(Doohickey)
        register_resource(Doohickey)
        assert get_registered_resources().count(Doohickey) == 1

    def test_register_rejects_non_resource(self):
        with pytest.raises(TypeError, match=r"BaseResource subclass"):
            register_resource(int)  # type: ignore[arg-type]

    def test_register_preserves_order(self):
        class Alpha(BaseResource):
            id: int

        class Beta(BaseResource):
            id: int

        register_resource(Beta)
        register_resource(Alpha)
        resources = get_registered_resources()
        assert resources.index(Beta) < resources.index(Alpha)

    def test_clear_registry(self):
        class Ephemeral(BaseResource):
            id: int

        register_resource(Ephemeral)
        assert Ephemeral in get_registered_resources()
        clear_registry()
        assert get_registered_resources() == []


class TestSyncDatabaseUrl:
    def test_asyncpg_to_psycopg2(self):
        assert (
            migrate_runner._sync_database_url("postgresql+asyncpg://u:p@h:5432/db")
            == "postgresql+psycopg2://u:p@h:5432/db"
        )

    def test_aiosqlite_to_sqlite(self):
        assert (
            migrate_runner._sync_database_url("sqlite+aiosqlite:///./db.sqlite")
            == "sqlite:///./db.sqlite"
        )

    def test_unknown_driver_passthrough(self):
        assert (
            migrate_runner._sync_database_url("mysql+pymysql://u:p@h/db")
            == "mysql+pymysql://u:p@h/db"
        )

    def test_already_sync_passthrough(self):
        assert migrate_runner._sync_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


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
        assert "import_resource_modules" in migrate_runner.ENV_PY_SOURCE

    def test_sets_resource_modules_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_MODULES_ENV, raising=False)
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        migrate_runner.build_alembic_config(
            cfg, database_url="sqlite:///./x.db", resource_modules=["a.b", "c.d"]
        )
        assert os.environ[_MODULES_ENV] == "a.b,c.d"

    def test_none_resource_modules_leaves_env_untouched(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_MODULES_ENV, raising=False)
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        migrate_runner.build_alembic_config(cfg, database_url="sqlite:///./x.db")
        assert _MODULES_ENV not in os.environ


class TestImportResourceModules:
    def test_csv_imports_and_registers(self, monkeypatch):
        monkeypatch.setenv(_MODULES_ENV, _MODULE_A)
        migrate_runner.import_resource_modules()
        assert "widgets" in ResourceyBase.metadata.tables
        assert any(c.__name__ == "Widget" for c in get_registered_resources())

    def test_sequential_indices_import_multiple(self, monkeypatch):
        monkeypatch.delenv(_MODULES_ENV, raising=False)
        monkeypatch.setenv(f"{_MODULES_ENV}_0", _MODULE_A)
        monkeypatch.setenv(f"{_MODULES_ENV}_1", _MODULE_B)
        migrate_runner.import_resource_modules()
        assert "widgets" in ResourceyBase.metadata.tables
        assert "gadgets" in ResourceyBase.metadata.tables

    def test_empty_env_no_error(self, monkeypatch):
        monkeypatch.delenv(_MODULES_ENV, raising=False)
        migrate_runner.import_resource_modules()  # no modules -> no-op

    def test_unimportable_module_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(_MODULES_ENV, "no_such_module_xyz")
        from resourcey.resource.errors import ResourceyConfigError

        with pytest.raises(ResourceyConfigError, match=r"no_such_module_xyz"):
            migrate_runner.import_resource_modules()


class TestRoundTrip:
    def test_generate_upgrade_downgrade(self, tmp_path, isolated_db):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path, [_MODULE_A])
        set_config(cfg)
        try:
            path = migrate_runner.generate(
                cfg.migrations,
                database_url=db_url,
                message="init",
                resource_modules=cfg.resource_modules,
            )
            assert Path(path).is_file()
            assert "create_table" in Path(path).read_text()

            migrate_runner.upgrade(
                cfg.migrations, database_url=db_url, resource_modules=cfg.resource_modules
            )
            assert "widgets" in _tables(db_file)
            assert _columns(db_file, "widgets") == ["id", "label", "created_at"]

            migrate_runner.downgrade(
                cfg.migrations,
                database_url=db_url,
                revision="base",
                resource_modules=cfg.resource_modules,
            )
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()

    def test_upgrade_head_then_downgrade_one(self, tmp_path, isolated_db):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path, [_MODULE_A])
        set_config(cfg)
        try:
            migrate_runner.generate(
                cfg.migrations,
                database_url=db_url,
                message="first",
                resource_modules=cfg.resource_modules,
            )
            migrate_runner.upgrade(
                cfg.migrations, database_url=db_url, resource_modules=cfg.resource_modules
            )
            assert "widgets" in _tables(db_file)

            migrate_runner.downgrade(
                cfg.migrations,
                database_url=db_url,
                revision="-1",
                resource_modules=cfg.resource_modules,
            )
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()


class TestAddColumn:
    def test_generate_add_column_and_rollback(self, tmp_path, isolated_db):
        db_url, db_file = isolated_db
        cfg, _ = _config(tmp_path, [_MODULE_A])
        set_config(cfg)
        try:
            migrate_runner.generate(
                cfg.migrations,
                database_url=db_url,
                message="v1",
                resource_modules=cfg.resource_modules,
            )
            migrate_runner.upgrade(
                cfg.migrations, database_url=db_url, resource_modules=cfg.resource_modules
            )
            assert "weight" not in _columns(db_file, "widgets")

            # Simulate the resource gaining a ``weight`` field: append a column
            # to the materialised Table in metadata. Autogeneration then diffs
            # the metadata table against the live DB and emits ``add_column``.
            # (Switching resource modules is impractical here because two
            # resources named ``Widget`` collide in SQLAlchemy's class
            # registry; direct table mutation exercises the same code path.)
            from sqlalchemy import Column, Float

            table = ResourceyBase.metadata.tables["widgets"]
            table.append_column(Column("weight", Float, nullable=True))

            p2 = migrate_runner.generate(
                cfg.migrations,
                database_url=db_url,
                message="add weight",
                resource_modules=cfg.resource_modules,
            )
            text2 = Path(p2).read_text()
            assert "add_column" in text2
            assert "weight" in text2

            migrate_runner.upgrade(
                cfg.migrations, database_url=db_url, resource_modules=cfg.resource_modules
            )
            assert "weight" in _columns(db_file, "widgets")

            migrate_runner.downgrade(
                cfg.migrations,
                database_url=db_url,
                revision="-1",
                resource_modules=cfg.resource_modules,
            )
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

    def test_generate_with_no_models_returns_pass_revision(self, tmp_path, isolated_db):
        # No resource modules -> metadata empty -> revision body is ``pass``.
        db_url, _ = isolated_db
        cfg = MigrationConfig(migrations_dir=str(tmp_path / "migs"))
        path = migrate_runner.generate(
            cfg, database_url=db_url, message="empty", resource_modules=[]
        )
        body = Path(path).read_text()
        assert "pass" in body


class TestCli:
    def test_parser_requires_subcommand(self):
        parser = migrate_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_autogenerate_requires_message(self):
        parser = migrate_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["migrate", "autogenerate"])

    def test_generate_alias_accepted(self):
        parser = migrate_cli.build_parser()
        args = parser.parse_args(["migrate", "generate", "-m", "x"])
        assert args.message == "x"

    def test_init_via_cli(self, tmp_path, monkeypatch):
        cfg = FrameworkConfig(
            migrations=MigrationConfig(migrations_dir=str(tmp_path / "migs")),
        )
        set_config(cfg)
        monkeypatch.setattr("resourcey.migrate.migrate_cli.get_config", lambda: cfg)
        try:
            rc = migrate_cli.main(["migrate", "init"])
            assert rc == 0
            assert (tmp_path / "migs" / "env.py").is_file()
        finally:
            clear_config_cache()

    def test_full_cli_round_trip(self, tmp_path, isolated_db, monkeypatch):
        db_url, db_file = isolated_db
        cfg = FrameworkConfig(
            migrations=MigrationConfig(migrations_dir=str(tmp_path / "migs")),
            resource_modules=[_MODULE_A],
        )
        # ``DbConfig.database_url`` assembles ``protocol://user:pass@host:port/db``
        # which is invalid for sqlite; monkeypatch the property to return the
        # temp-file URL the CLI reads.
        monkeypatch.setattr(type(cfg.database), "database_url", property(lambda self: db_url))
        set_config(cfg)
        try:
            assert migrate_cli.main(["migrate", "autogenerate", "-m", "init"]) == 0
            assert migrate_cli.main(["migrate", "upgrade"]) == 0
            assert "widgets" in _tables(db_file)
            assert migrate_cli.main(["migrate", "downgrade", "base"]) == 0
            assert "widgets" not in _tables(db_file)
        finally:
            clear_config_cache()


class TestEnvDrivenConfig:
    def test_resource_modules_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RESOURCEY_MIGRATIONS_MIGRATIONS_DIR", str(tmp_path / "envmigs"))
        monkeypatch.setenv("RESOURCEY_RESOURCE_MODULES", f'["{_MODULE_A}"]')
        FrameworkConfig.clear_instance_cache()
        try:
            cfg = FrameworkConfig.get_instance()
            assert cfg.migrations.migrations_dir == str(tmp_path / "envmigs")
            assert cfg.resource_modules == [_MODULE_A]
        finally:
            FrameworkConfig.clear_instance_cache()

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
