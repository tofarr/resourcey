"""Tests for the ``resourcey`` CLI dispatcher (issue #51).

The ``run`` command was removed (apps start via ``uvicorn``). The CLI now
dispatches ``migrate`` and prints help when invoked bare. These tests cover
the dispatcher behaviour and the console-script entry point.
"""

from __future__ import annotations

import pytest

from resourcey.cli import main as cli_main


class TestDispatcher:
    def test_bare_invocation_prints_help(self):
        # No subcommand -> help printed, returns 0.
        rc = cli_main([])
        assert rc == 0

    def test_unknown_command_errors(self):
        with pytest.raises(SystemExit):
            cli_main(["nope"])

    def test_migrate_requires_subcommand(self):
        with pytest.raises(SystemExit):
            cli_main(["migrate"])


class TestMigrateSubcommand:
    def test_autogenerate_requires_message(self):
        with pytest.raises(SystemExit):
            cli_main(["migrate", "autogenerate"])

    def test_generate_alias_accepted(self, tmp_path, monkeypatch):
        from resourcey.config.config_framework import FrameworkConfig, MigrationConfig
        from resourcey.config.config_runtime import clear_config_cache, set_config
        from resourcey.migrate import migrate_runner

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


class TestConsoleScriptImport:
    def test_console_script_callable_exists(self):
        assert callable(cli_main)
