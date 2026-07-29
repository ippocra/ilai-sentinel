# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Sentinel CLI argument parser."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the source tree is on sys.path so the module can be imported.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from sentinel.config import DEFAULT_CONFIG_PATH, Config
from sentinel.cli import ServerURL, build_parser, cmd_probe_llm


# ── Parser construction ───────────────────────────────────────────


class TestParserConstruction:
    """Verify that the parser can be built and exposes expected subcommands."""

    def test_parser_has_expected_subcommands(self):
        parser = build_parser()
        # Find the SubParsersAction (the one with a dict of choices).
        sub_actions = [
            a
            for a in parser._actions
            if hasattr(a, "choices") and isinstance(a.choices, dict)
        ]
        assert len(sub_actions) == 1
        choices = sub_actions[0].choices
        expected = {
            "enroll",
            "run-once",
            "probe-llm",
            "collect-hardware",
            "daemon",
            "service",
            "status",
        }
        assert set(choices.keys()) == expected

    def test_parser_has_global_options(self):
        parser = build_parser()
        option_strings = [
            flag for action in parser._actions for flag in action.option_strings
        ]
        assert "--config" in option_strings
        assert "--log-level" in option_strings
        assert "-h" in option_strings or "--help" in option_strings


# ── ServerURL type ────────────────────────────────────────────────


class TestServerURL:
    def test_valid_http(self):
        result = ServerURL("http://example.com")
        assert str(result) == "http://example.com"

    def test_valid_https(self):
        result = ServerURL("https://mothership.example.com/api")
        assert str(result) == "https://mothership.example.com/api"

    def test_invalid_no_scheme(self):
        with pytest.raises(Exception) as exc_info:
            ServerURL("mothership.example.com")
        assert "must include a scheme" in str(exc_info.value)

    def test_invalid_ftp_scheme(self):
        with pytest.raises(Exception) as exc_info:
            ServerURL("ftp://example.com")
        assert "must include a scheme" in str(exc_info.value)


# ── Subcommand argument parsing ───────────────────────────────────


class TestSubcommandParsing:
    def test_enroll_requires_server_and_code(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["enroll", "--code", "ABCD"])
        assert exc.value.code == 2

    def test_enroll_server_validation(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["enroll", "--server", "badurl", "--code", "XYZ"])
        assert exc.value.code == 2

    def test_enroll_parsed(self):
        parser = build_parser()
        args = parser.parse_args(
            ["enroll", "--server", "https://example.com", "--code", "ABCD"]
        )
        assert args.command == "enroll"
        assert args.server == "https://example.com"
        assert args.code == "ABCD"

    def test_probe_llm_default_ports(self):
        parser = build_parser()
        args = parser.parse_args(["probe-llm"])
        assert args.ports is None

    def test_probe_llm_custom_ports(self):
        parser = build_parser()
        args = parser.parse_args(["probe-llm", "--ports", "8888", "8000"])
        assert args.ports == [8888, 8000]

    def test_probe_llm_urls(self):
        parser = build_parser()
        args = parser.parse_args(
            ["probe-llm", "--urls", "http://a", "http://b"]
        )
        assert args.urls == ["http://a", "http://b"]

    def test_service_requires_action(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["service"])
        assert exc.value.code == 2

    def test_service_invalid_action(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["service", "--action", "foo"])
        assert exc.value.code == 2

    def test_service_install_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["service", "--action", "install"])
        assert args.command == "service"
        assert args.action == "install"

    def test_run_once_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["run-once"])
        assert args.command == "run-once"

    def test_collect_hardware_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["collect-hardware"])
        assert args.command == "collect-hardware"

    def test_daemon_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["daemon"])
        assert args.command == "daemon"

    def test_status_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_global_config_on_subcommand(self):
        # --config and --log-level are top-level options. They must be placed
        # before the subcommand name in argparse's positional-subparser model.
        parser = build_parser()
        args = parser.parse_args(
            ["--config", "/tmp/test.toml", "--log-level", "DEBUG", "daemon"]
        )
        assert args.command == "daemon"
        assert args.config == "/tmp/test.toml"
        assert args.log_level == "DEBUG"

    def test_no_command_shows_help(self):
        parser = build_parser()
        # argparse raises SystemExit(0) for --help, but for missing command
        # it also prints help and exits with 0 (since we handle it in main).
        # parse_args alone just sets dest=None; main() handles printing.
        args = parser.parse_args([])
        assert args.command is None

    def test_help_flag_exits_0(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--help"])
        assert exc.value.code == 0

    def test_subcommand_help_exits_0(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["enroll", "--help"])
        assert exc.value.code == 0


# ── Function dispatch ─────────────────────────────────────────────


class TestFunctionDispatch:
    """Verify that each subparser is wired to the correct command function."""

    def _get_func(self, subcommand: str):
        parser = build_parser()
        # Find the SubParsersAction.
        sub_actions = [
            a
            for a in parser._actions
            if hasattr(a, "choices") and isinstance(a.choices, dict)
        ]
        return sub_actions[0].choices[subcommand]._defaults["func"]

    def test_enroll_func(self):
        from sentinel.cli import cmd_enroll

        assert self._get_func("enroll") is cmd_enroll

    def test_run_once_func(self):
        from sentinel.cli import cmd_run_once

        assert self._get_func("run-once") is cmd_run_once

    def test_probe_llm_func(self):
        assert self._get_func("probe-llm") is cmd_probe_llm

    def test_collect_hardware_func(self):
        from sentinel.cli import cmd_collect_hardware

        assert self._get_func("collect-hardware") is cmd_collect_hardware

    def test_daemon_func(self):
        from sentinel.cli import cmd_daemon

        assert self._get_func("daemon") is cmd_daemon

    def test_service_func(self):
        from sentinel.cli import cmd_service

        assert self._get_func("service") is cmd_service

    def test_status_func(self):
        from sentinel.cli import cmd_status

        assert self._get_func("status") is cmd_status


# ── Config path consistency ───────────────────────────────────────


class TestConfigPathConsistency:
    """Commands should agree on the same implicit config file."""

    def test_enroll_persists_to_explicit_config_path(self, tmp_path, monkeypatch):
        from sentinel import cli

        token_file = tmp_path / "device.token"
        queue_file = tmp_path / "queue.db"
        backup_dir = tmp_path / "backups"
        explicit_config = tmp_path / "custom.toml"
        saved_paths = []

        config = Config()
        config.auth.token_file = str(token_file)
        config.queue.path = str(queue_file)
        config.backup.workdir = str(backup_dir)

        class FakeClient:
            def __init__(self, server_url, token):
                self.server_url = server_url
                self.token = token

            def enroll(self, **kwargs):
                return {"device_token": "secret-token", "device_id": "device-123"}

        def fake_save_config(config, path=None):
            saved_paths.append(path)
            return path

        monkeypatch.setattr(cli, "load_config", lambda config_path=None: config)
        monkeypatch.setattr(cli, "collect_raw", lambda: {"raw": {"hermes_version": "test"}})
        monkeypatch.setattr(cli, "SentinelClient", FakeClient)
        monkeypatch.setattr(cli, "save_config", fake_save_config)

        args = argparse.Namespace(
            config=str(explicit_config), server="https://example.com", code="ABCD"
        )

        cli.cmd_enroll(args)

        assert saved_paths == [explicit_config]

    def test_status_reports_same_default_config_path_used_by_loader(
        self, tmp_path, monkeypatch, capsys
    ):
        from sentinel import cli

        config = Config()
        config.auth.token_file = str(tmp_path / "device.token")
        config.queue.path = str(tmp_path / "queue.db")

        monkeypatch.setattr(cli, "load_config", lambda config_path=None: config)

        cli.cmd_status(argparse.Namespace(config=None))

        assert f"Config: {DEFAULT_CONFIG_PATH}" in capsys.readouterr().out

    def test_service_install_uses_same_default_config_path(self, capsys):
        from sentinel import cli

        cli.cmd_service(argparse.Namespace(config=None, action="install"))

        assert f"ExecStart=/usr/local/bin/sentinel daemon --config {DEFAULT_CONFIG_PATH}" in (
            capsys.readouterr().out
        )


# ── CLI integration (no network) ──────────────────────────────────


class TestCLIIntegration:
    """End-to-end tests using subprocess — no network calls expected."""

    @pytest.fixture(autouse=True)
    def _set_path(self):
        env = {"PYTHONPATH": str(SRC)}
        # Use the venv if available
        venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
        if venv.exists():
            self._python = str(venv)
        else:
            self._python = sys.executable
        self._env = env

    def test_cli_help(self):
        result = subprocess.run(
            [self._python, "-m", "sentinel.cli", "--help"],
            capture_output=True,
            text=True,
            env=self._env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        assert "Ippocra ILAI Sentinel" in result.stdout
        assert "--config" in result.stdout
        assert "--log-level" in result.stdout

    def test_cli_enroll_help(self):
        result = subprocess.run(
            [self._python, "-m", "sentinel.cli", "enroll", "--help"],
            capture_output=True,
            text=True,
            env=self._env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        assert "--server" in result.stdout
        assert "--code" in result.stdout

    def test_cli_collect_hardware_json(self):
        result = subprocess.run(
            [self._python, "-m", "sentinel.cli", "collect-hardware"],
            capture_output=True,
            text=True,
            env=self._env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        import json

        data = json.loads(result.stdout)
        assert "timestamp" in data
        assert "hostname" in data
        assert "hardware" in data

    def test_cli_bad_server_rejected(self):
        result = subprocess.run(
            [
                self._python,
                "-m",
                "sentinel.cli",
                "enroll",
                "--server",
                "not-a-url",
                "--code",
                "ABC",
            ],
            capture_output=True,
            text=True,
            env=self._env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode != 0
        assert "must include a scheme" in result.stderr

    def test_cli_no_command_shows_help(self):
        result = subprocess.run(
            [self._python, "-m", "sentinel.cli"],
            capture_output=True,
            text=True,
            env=self._env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        assert "Available commands" in result.stdout
