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

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    assert hardware.get_hermes_version() == "0.21.0"
    assert calls == [["hermes", "--version"]]


def test_hermes_version_prefers_cli_over_stale_import(monkeypatch):
    """A stale/empty hermes_cli.__version__ must not mask the CLI (campus bug)."""
    from sentinel import hardware

    class FakeResult:
        returncode = 0
        stdout = "Hermes Agent v0.21.0 (2026.8.31) · upstream 63279301\n"

    monkeypatch.setattr(
        hardware.subprocess, "run", lambda *a, **k: FakeResult()
    )

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
