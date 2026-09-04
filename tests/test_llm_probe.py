# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for LLM backend probing."""

from __future__ import annotations

import argparse

from sentinel import llm_probe
from sentinel.config import Config


def test_parser_accepts_labeled_rate_metrics_and_ignores_counters():
    metrics = """
# HELP llama_tokens_total total
llama_tokens_total 999
llama_tokens_per_second{slot=\"0\"} 18.5
llama_generation_rate 11.0
"""
    assert llm_probe._parse_llama_metrics_tokens_per_sec(metrics) == 18.5


def test_parser_prefers_generation_throughput_over_prompt_throughput():
    metrics = """
llamacpp:prompt_tokens_seconds 716.925
llamacpp:predicted_tokens_seconds 54.0077
"""
    assert llm_probe._parse_llama_metrics_tokens_per_sec(metrics) == 54.0077


def test_probe_reads_metrics_for_loaded_model_on_router(monkeypatch):
    def fake_probe_url(url, timeout=3.0):
        if url.endswith("/props"):
            return {"role": "router", "model_path": "none"}
        if url.endswith("/v1/models"):
            return {
                "data": [
                    {"id": "unloaded", "status": {"value": "unloaded"}},
                    {"id": "qwen", "status": {"value": "loaded"}},
                ]
            }
        return None

    def fake_probe_text_url(url, timeout=3.0):
        if url == "http://127.0.0.1:8013/metrics?model=qwen":
            return "llamacpp:predicted_tokens_seconds 54.0077\n"
        return None

    monkeypatch.setattr(llm_probe, "_probe_url", fake_probe_url)
    monkeypatch.setattr(llm_probe, "_probe_text_url", fake_probe_text_url)

    result = llm_probe.probe([8013], [])

    assert result["backends"][0]["model"] == "qwen"
    assert result["backends"][0]["tokens_per_sec"] == 54.0077
    assert result["backends"][0]["throughput_status"] == "reported"


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
            "tokens_per_sec": None,
            "throughput_status": "unavailable",
            "tokens_in_total": 0,
            "tokens_out_total": 0,
            "generation_tps": None,
            "prompt_tps": None,
            "requests_processing": None,
            "requests_deferred": None,
            "n_tokens_max": None,
            "slots": [],
        }
    ]
    assert result["detected_backends"] == ["openai-compatible"]


def test_probe_uses_loaded_openai_model_instead_of_first_result(monkeypatch):
    def fake_probe_url(url, timeout=3.0):
        if url.endswith("/props"):
            return {"role": "router", "model_path": "none"}
        if url.endswith("/v1/models"):
            return {
                "data": [
                    {"id": "bonsai", "status": {"value": "unloaded"}},
                    {"id": "qwen-default", "status": {"value": "loaded"}},
                ]
            }
        return None

    monkeypatch.setattr(llm_probe, "_probe_url", fake_probe_url)

    result = llm_probe.probe([8013], [])

    assert result["backends"][0]["model"] == "qwen-default"


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


def test_cumulative_tokens_sums_across_labels():
    metrics = (
        "llamacpp:prompt_tokens_total 12500\n"
        "llamacpp:prompt_tokens_total{backend=\"B\"} 500\n"
        "llamacpp:tokens_predicted_total 34000\n"
        "llamacpp:tokens_predicted_total{backend=\"B\"} 900\n"
    )
    assert llm_probe._parse_llama_metrics_cumulative_tokens(metrics) == (13000, 34900)
    # Unlabeled lines must still parse (regression: name had value appended).
    assert llm_probe._parse_llama_metrics_cumulative_tokens("llamacpp:prompt_tokens_total 7\n") == (7, 0)
    # No metrics -> zeros.
    assert llm_probe._parse_llama_metrics_cumulative_tokens(None) == (0, 0)


def test_gauges_take_max_across_labels_and_parse_unlabeled():
    metrics = (
        "llamacpp:tokens_predicted_seconds 54.0\n"
        "llamacpp:tokens_predicted_seconds{backend=\"A\"} 50.1\n"
        "llamacpp:tokens_predicted_seconds{backend=\"B\"} 58.9\n"
        "llamacpp:prompt_tokens_seconds 420.5\n"
        "llamacpp:requests_processing 2\n"
        "llamacpp:requests_deferred 1\n"
        "llamacpp:n_tokens_max 4096\n"
    )
    gauges = llm_probe._parse_llama_metrics_gauges(metrics)
    assert gauges["generation_tps"] == 58.9
    assert gauges["prompt_tps"] == 420.5
    assert gauges["requests_processing"] == 2.0
    assert gauges["requests_deferred"] == 1.0
    assert gauges["n_tokens_max"] == 4096.0
    # Empty metrics -> all None.
    assert all(v is None for v in llm_probe._parse_llama_metrics_gauges(None).values())
