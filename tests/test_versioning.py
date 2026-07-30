# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for VCS-derived package versioning."""

from __future__ import annotations

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