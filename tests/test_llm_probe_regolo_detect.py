# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for auto-detecting Regolo from ~/.ilai/cloud/regolo.env."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel import llm_probe


@pytest.fixture
def regolo_home(tmp_path, monkeypatch):
    """Point HOME at a temp dir and create the cloud env file ilai writes."""
    home = tmp_path / "home"
    cloud_dir = home / ".ilai" / "cloud"
    cloud_dir.mkdir(parents=True)
    env_file = cloud_dir / "regolo.env"
    monkeypatch.setenv("HOME", str(home))
    return home, cloud_dir, env_file


def _write(env_file: Path, text: str) -> None:
    env_file.write_text(text)


def test_detect_parses_quoted_values(regolo_home):
    home, cloud_dir, env_file = regolo_home
    _write(
        env_file,
        'REGOLO_API_KEY="reg-abc123"\n'
        'REGOLO_BASE_URL="https://api.regolo.ai/v1"\n'
        'REGOLO_MODEL="qwen3.8-27b"\n',
    )

    detected = llm_probe.detect_remote_from_env()

    assert detected == {
        "remote_url": "https://api.regolo.ai/v1",
        "remote_api_key": "reg-abc123",
        "remote_model": "qwen3.8-27b",
    }


def test_detect_missing_file_returns_none(regolo_home):
    _, _, env_file = regolo_home
    assert llm_probe.detect_remote_from_env() is None


def test_detect_missing_key_returns_none(regolo_home):
    _, _, env_file = regolo_home
    _write(env_file, "REGOLO_BASE_URL=https://api.regolo.ai/v1\n")
    assert llm_probe.detect_remote_from_env() is None


def test_detect_defaults_base_url_when_absent(regolo_home):
    _, _, env_file = regolo_home
    _write(env_file, 'REGOLO_API_KEY="reg-xyz"\n')

    detected = llm_probe.detect_remote_from_env()

    assert detected["remote_url"] == "https://api.regolo.ai/v1"
    assert detected["remote_api_key"] == "reg-xyz"
    assert detected["remote_model"] == ""


def test_detect_ignores_comments_and_blank_lines(regolo_home):
    _, _, env_file = regolo_home
    _write(
        env_file,
        "# Regolo credentials\n"
        "\n"
        'REGOLO_API_KEY="reg-c1"\n'
        "\n",
    )

    detected = llm_probe.detect_remote_from_env()

    assert detected["remote_api_key"] == "reg-c1"


def test_detect_tolerates_unquoted_values(regolo_home):
    _, _, env_file = regolo_home
    _write(
        env_file,
        "REGOLO_API_KEY=reg-bare\n"
        "REGOLO_BASE_URL=https://api.regolo.ai/v1\n",
    )

    detected = llm_probe.detect_remote_from_env()

    assert detected["remote_api_key"] == "reg-bare"


def test_detect_uses_explicit_path(monkeypatch, tmp_path):
    """Explicit path wins over the default location."""
    custom = tmp_path / "custom.env"
    custom.write_text('REGOLO_API_KEY="reg-explicit"\n')

    detected = llm_probe.detect_remote_from_env(custom)

    assert detected is not None
    assert detected["remote_api_key"] == "reg-explicit"


def test_detect_path_not_found_returns_none(tmp_path):
    assert llm_probe.detect_remote_from_env(tmp_path / "nope.env") is None


def test_probe_auto_detects_remote_from_env_file(regolo_home, monkeypatch):
    """probe(auto_detect_remote=True) picks up Regolo from the env file when
    no remote_url is configured explicitly."""
    home, cloud_dir, env_file = regolo_home
    _write(
        env_file,
        'REGOLO_API_KEY="reg-auto"\n'
        'REGOLO_BASE_URL="https://api.regolo.ai/v1"\n'
        'REGOLO_MODEL="qwen3.8-27b"\n',
    )

    def fake_remote(url, api_key, model="", timeout=5.0):
        return {
            "backend": "regolo",
            "url": url,
            "model": model or "qwen3.8-27b",
            "status": "ok",
        }

    monkeypatch.setattr(llm_probe, "probe_remote", fake_remote)
    monkeypatch.setattr(llm_probe, "_probe_url", lambda url, timeout=3.0: None)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)

    result = llm_probe.probe(auto_detect_remote=True)

    regolo = [b for b in result["backends"] if b["backend"] == "regolo"]
    assert len(regolo) == 1
    assert regolo[0]["url"] == "https://api.regolo.ai/v1"
    assert "regolo" in result["detected_backends"]


def test_probe_explicit_remote_wins_over_env_file(regolo_home, monkeypatch):
    """An explicitly configured remote_url takes precedence over the env file."""
    home, cloud_dir, env_file = regolo_home
    _write(
        env_file,
        'REGOLO_API_KEY="reg-from-file"\n'
        'REGOLO_BASE_URL="https://file.regolo.ai/v1"\n',
    )

    seen = {}

    def fake_remote(url, api_key, model="", timeout=5.0):
        seen["url"] = url
        seen["key"] = api_key
        return {"backend": "regolo", "url": url, "model": "m", "status": "ok"}

    monkeypatch.setattr(llm_probe, "probe_remote", fake_remote)
    monkeypatch.setattr(llm_probe, "_probe_url", lambda url, timeout=3.0: None)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)

    llm_probe.probe(
        remote_url="https://explicit.regolo.ai/v1",
        remote_api_key="reg-explicit",
        auto_detect_remote=True,
    )

    assert seen["url"] == "https://explicit.regolo.ai/v1"
    assert seen["key"] == "reg-explicit"
