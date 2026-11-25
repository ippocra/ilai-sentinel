# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for VCS-derived package versioning."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_project_uses_hatch_vcs_dynamic_versioning():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert data["project"]["dynamic"] == ["version"]
    assert "version" not in data["project"]
    assert data["tool"]["hatch"]["version"] == {"source": "vcs"}
    assert data["tool"]["hatch"]["build"]["hooks"]["vcs"] == {
        "version-file": "src/sentinel/_version.py"
    }
    assert "hatch-vcs" in data["build-system"]["requires"]


def test_runtime_version_is_resolved():
    import sentinel

    assert sentinel.__version__
    assert sentinel.__version__ != "0.1.0"
    assert sentinel.__version__ != "0.1.1"


def test_hermes_version_uses_cli_global_option(monkeypatch):
    from sentinel import hardware

    calls = []

    class FakeResult:
        returncode = 0
        stdout = "Hermes Agent v0.21.0 (2026.8.31) · upstream abc123\n"

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeResult()

    # Resolve `hermes` to a plain PATH name (no home-dir fallback exercised).
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "hermes")
    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert hardware.get_hermes_version() == "0.21.0"
    assert calls == [["hermes", "--version"]]


def test_hermes_version_resolves_executable_outside_minimal_service_path(
    monkeypatch, tmp_path
):
    """The systemd user service PATH omits $HOME/.local/bin, where user-local
    installs of `hermes` live (fido box bug: version reported stale/empty)."""
    from sentinel import hardware

    # Fake $HOME with a user-local launcher, not on PATH.
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "hermes"
    launcher.write_text("#!/bin/sh\necho v1\n")
    launcher.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hardware.shutil, "which", lambda _: None)

    class FakeResult:
        returncode = 0
        stdout = "Hermes Agent v0.23.4 (2026.9.24) · stable · local abc123\n"

    def fake_run(command, **kwargs):
        assert command[0] == str(launcher), "must use the resolved absolute path"
        assert command[1:] == ["--version"]
        return FakeResult()

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert hardware.get_hermes_version() == "0.23.4"


def test_hermes_version_prefers_which_resolution_over_home_fallback(monkeypatch):
    """When `hermes` IS on PATH, shutil.which wins over the home-dir probes."""
    from sentinel import hardware

    monkeypatch.setenv("HOME", "/nonexistent-home")

    class FakeResult:
        returncode = 0
        stdout = "Hermes Agent v0.20.0 (2026.8.01) · upstream abc123\n"

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeResult()

    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/hermes")
    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert hardware.get_hermes_version() == "0.20.0"
    assert calls == [["/usr/bin/hermes", "--version"]]


def test_hermes_version_returns_empty_when_no_executable_found(monkeypatch):
    """No hermes on PATH and no home-dir install → empty string, no crash."""
    from sentinel import hardware

    monkeypatch.setenv("HOME", "/nonexistent-home")
    monkeypatch.setattr(hardware.shutil, "which", lambda _: None)

    def raise_missing(*a, **k):
        raise AssertionError("subprocess must not be invoked")

    monkeypatch.setattr(hardware.subprocess, "run", raise_missing)

    assert hardware.get_hermes_version() == ""


def test_hermes_version_prefers_cli_over_stale_import(monkeypatch):
    """A stale/empty hermes_cli.__version__ must not mask the CLI (campus bug)."""
    from sentinel import hardware

    class FakeResult:
        returncode = 0
        stdout = "Hermes Agent v0.21.0 (2026.8.31) · upstream 63279301\n"

    monkeypatch.setattr(
        hardware.subprocess, "run", lambda *a, **k: FakeResult()
    )
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "hermes")

    class StaleHermesCli:
        __version__ = ""  # empty, as seen on the medical campus box

    import types

    monkeypatch.setitem(
        sys.modules, "hermes_cli", types.SimpleNamespace(__version__="")
    )

    assert hardware.get_hermes_version() == "0.21.0"


def test_hermes_version_falls_back_to_import_when_cli_missing(monkeypatch):
    from sentinel import hardware

    def raise_missing(*a, **k):
        raise FileNotFoundError("hermes not found")

    monkeypatch.setattr(hardware.subprocess, "run", raise_missing)

    import types

    monkeypatch.setitem(
        sys.modules, "hermes_cli", types.SimpleNamespace(__version__="0.19.0")
    )

    assert hardware.get_hermes_version() == "0.19.0"
