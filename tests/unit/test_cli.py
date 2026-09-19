"""Tests for ``resourcey.cli`` and ``resourcey.__main__`` (issue #21).

Covers the ``resourcey run`` command (host/port override resolution, default
behaviour) and the console-script entry point, using a stubbed ``uvicorn.run``
so no server is actually started. ``python -m resourcey`` is exercised too.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from resourcey.cli import main
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import clear_config_cache


@pytest.fixture(autouse=True)
def _reset_config():
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()
    yield
    clear_config_cache()
    FrameworkConfig.clear_instance_cache()


def _run_cli(argv, monkeypatch):
    """Invoke the CLI with a stubbed uvicorn.run, returning the (captured) call."""
    import uvicorn

    with patch.object(uvicorn, "run", lambda *args, **kwargs: captured.append(kwargs)):
        captured: list[dict] = []
        code = main(argv)
    return code, captured


class TestRunCommand:
    def test_uses_config_host_and_port(self, monkeypatch):
        monkeypatch.delenv("RESOURCEY_HOST", raising=False)
        monkeypatch.delenv("RESOURCEY_PORT", raising=False)
        code, captured = _run_cli(["run"], monkeypatch)
        assert code == 0
        kwargs = captured[0]
        assert kwargs["factory"] is True
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8000
        assert kwargs["reload"] is False

    def test_host_port_from_config_env(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_HOST", "0.0.0.0")
        monkeypatch.setenv("RESOURCEY_PORT", "9000")
        FrameworkConfig.clear_instance_cache()
        _, captured = _run_cli(["run"], monkeypatch)
        assert captured[0]["host"] == "0.0.0.0"
        assert captured[0]["port"] == 9000

    def test_cli_host_overrides_config(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_HOST", "0.0.0.0")
        monkeypatch.setenv("RESOURCEY_PORT", "9000")
        FrameworkConfig.clear_instance_cache()
        _, captured = _run_cli(["run", "--host", "1.2.3.4"], monkeypatch)
        assert captured[0]["host"] == "1.2.3.4"
        # Port still from config.
        assert captured[0]["port"] == 9000

    def test_cli_port_overrides_config(self, monkeypatch):
        monkeypatch.setenv("RESOURCEY_PORT", "9000")
        FrameworkConfig.clear_instance_cache()
        _, captured = _run_cli(["run", "--port", "4242"], monkeypatch)
        assert captured[0]["port"] == 4242

    def test_reload_flag_passed_through(self, monkeypatch):
        _, captured = _run_cli(["run", "--reload"], monkeypatch)
        assert captured[0]["reload"] is True

    def test_runs_factory_not_instance(self, monkeypatch):
        _, captured = _run_cli(["run"], monkeypatch)
        assert captured[0]["factory"] is True
        assert captured[0].get("app") is None


class TestDefaultCommand:
    def test_bare_invocation_runs(self, monkeypatch):
        # No subcommand -> default run behaviour.
        code, captured = _run_cli([], monkeypatch)
        assert code == 0
        assert captured  # uvicorn.run was called

    def test_unknown_command_errors(self, monkeypatch):
        # An unknown subcommand is rejected by argparse (SystemExit, code 2).
        with pytest.raises(SystemExit) as exc:
            main(["bogus"])
        assert exc.value.code == 2


class TestMainEntryPoint:
    def test_python_m_resourcey_runs(self, monkeypatch):
        # Simulate `python -m resourcey run` by invoking __main__'s main().
        import uvicorn

        import resourcey.__main__ as entry

        captured: list[dict] = []
        with patch.object(uvicorn, "run", lambda *args, **kwargs: captured.append(kwargs)):
            code = entry.main(["run"])
        assert code == 0
        assert captured


class TestConsoleScriptImport:
    def test_console_script_callable_exists(self):
        # The pyproject [project.scripts] entry points at resourcey.cli:main.
        import uvicorn

        from resourcey.cli import main as cli_main

        assert callable(cli_main)
        with patch.object(uvicorn, "run", lambda *args, **kwargs: None):
            code = cli_main(["run"])
        assert code == 0
