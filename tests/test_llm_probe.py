# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for LLM backend probing."""

from __future__ import annotations

import argparse

from sentinel import llm_probe
from sentinel.config import Config


def test_probe_ignores_ports_without_recognized_llm_endpoint(monkeypatch):
    monkeypatch.setattr(llm_probe, "_probe_url", lambda url, timeout=3.0: None)

    result = llm_probe.probe([8888], [])

    assert result == {"backends": [], "detected_backends": []}


def test_probe_uses_openai_models_when_props_has_no_model(monkeypatch):
    def fake_probe_url(url, timeout=3.0):
        if url.endswith("/props"):
            return {"role": "router", "model_path": "none"}
        if url.endswith("/v1/models"):
            return {"data": [{"id": "bonsai"}]}
        return None

    monkeypatch.setattr(llm_probe, "_probe_url", fake_probe_url)

    result = llm_probe.probe([8013], [])

    assert result["backends"] == [
        {
            "backend": "openai-compatible",
            "url": "http://127.0.0.1:8013",
            "model": "bonsai",
            "tokens_per_sec": 0.0,
            "slots": [],
        }
    ]
    assert result["detected_backends"] == ["openai-compatible"]


def test_probe_llm_command_uses_configured_ports_and_urls_by_default(monkeypatch, capsys):
    from sentinel import cli

    config = Config()
    config.llm.ports = [8013]
    config.llm.extra_urls = ["http://127.0.0.1:9999"]
    calls = []

    monkeypatch.setattr(cli, "load_config", lambda config_path=None: config)

    def fake_probe(ports, urls):
        calls.append((ports, urls))
        return {"backends": [], "detected_backends": []}

    monkeypatch.setattr(cli, "probe_llm", fake_probe)

    cli.cmd_probe_llm(argparse.Namespace(config=None, ports=None, urls=None))

    assert calls == [([8013], ["http://127.0.0.1:9999"])]
    assert '"backends": []' in capsys.readouterr().out
