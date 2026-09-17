# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for remote (cloud) LLM probing — e.g. Regolo on ILAI-on-Cloud boxes."""

from __future__ import annotations

import urllib.error

from sentinel import llm_probe


def _authed(monkeypatch, data=None, error=None):
    """Patch llm_probe._probe_url_auth (the auth seam used by probe_remote)."""
    def fake(url, api_key="", timeout=3.0):
        if error is not None:
            raise error
        return data
    monkeypatch.setattr(llm_probe, "_probe_url_auth", fake)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)


def test_probe_remote_returns_backend_entry(monkeypatch):
    """A healthy remote endpoint yields a backend entry with model resolved."""
    seen = {}

    def fake(url, api_key="", timeout=3.0):
        seen["url"] = url
        return {"data": [{"id": "gpt-4o"}]}

    monkeypatch.setattr(llm_probe, "_probe_url_auth", fake)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1/", api_key="k123", model="")

    assert result is not None
    assert result["backend"] == "regolo"
    assert result["url"] == "https://api.regolo.ai/v1"
    assert result["model"] == "gpt-4o"
    assert result["status"] == "ok"
    # /models was queried at the configured base URL
    assert seen["url"] == "https://api.regolo.ai/v1/models"


def test_probe_remote_sends_bearer_token(monkeypatch):
    """The API key travels as a Bearer token, never in the URL."""
    import io
    import json

    captured = {}

    def fake(req, timeout=3.0):
        captured["auth"] = req.headers.get("Authorization")
        captured["url"] = req.full_url
        return io.BytesIO(json.dumps({"data": [{"id": "m1"}]}).encode())

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="secret_key")

    assert result is not None
    assert captured["auth"] == "Bearer secret_key"
    assert "secret_key" not in captured["url"]


def test_probe_remote_401_maps_to_auth_failed(monkeypatch):
    """HTTP 401/403 -> status=auth_failed (key invalid or expired)."""
    _authed(monkeypatch, error=llm_probe.HTTPAuthError("401"))

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="bad")

    assert result is not None
    assert result["status"] == "auth_failed"
    assert result["model"] == ""


def test_probe_remote_403_maps_to_auth_failed(monkeypatch):
    _authed(monkeypatch, error=llm_probe.HTTPAuthError("403"))

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="bad")

    assert result is not None
    assert result["status"] == "auth_failed"


def test_probe_remote_connection_error_maps_to_unreachable(monkeypatch):
    _authed(monkeypatch, error=OSError("network unreachable"))

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="k")

    assert result is not None
    assert result["status"] == "unreachable"


def test_probe_remote_http_error_non_auth_maps_to_unreachable(monkeypatch):
    """A 500 from the provider is "unreachable", not an auth problem."""
    def fake(url, api_key="", timeout=3.0):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    monkeypatch.setattr(llm_probe, "_probe_url_auth", fake)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="k")

    assert result is not None
    assert result["status"] == "unreachable"


def test_probe_remote_empty_models_maps_to_no_models(monkeypatch):
    _authed(monkeypatch, data={"data": []})

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="k")

    assert result is not None
    assert result["status"] == "no_models"
    assert result["model"] == ""


def test_probe_remote_configured_model_wins_when_list_empty(monkeypatch):
    """If the operator pinned remote_model, report it even when the API
    returns no models (some providers list models lazily)."""
    _authed(monkeypatch, data={"data": []})

    result = llm_probe.probe_remote(
        url="https://api.regolo.ai/v1", api_key="k", model="gpt-4o"
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["model"] == "gpt-4o"


def test_probe_remote_selected_model_prefers_loaded(monkeypatch):
    _authed(
        monkeypatch,
        data={
            "data": [
                {"id": "unloaded", "status": {"value": "unloaded"}},
                {"id": "active-model", "status": {"value": "loaded"}},
            ]
        },
    )

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="k")

    assert result["model"] == "active-model"


def test_probe_remote_empty_url_returns_none():
    assert llm_probe.probe_remote(url="", api_key="k") is None


def test_probe_remote_no_metrics_gives_zero_counters(monkeypatch):
    """Remote APIs expose no Prometheus counters — cumulative fields are 0."""
    _authed(monkeypatch, data={"data": [{"id": "m1"}]})

    result = llm_probe.probe_remote(url="https://api.regolo.ai/v1", api_key="k")

    assert result["tokens_in_total"] == 0
    assert result["tokens_out_total"] == 0
    assert result["throughput_status"] == "unavailable"
    # Shape-compatible with local probe entries for the mothership serializer
    for field in (
        "backend", "url", "model", "tokens_per_sec",
        "tokens_in_total", "tokens_out_total", "slots",
    ):
        assert field in result


def test_probe_wires_remote_before_ports(monkeypatch):
    """probe() consults remote config first; a healthy remote endpoint is
    reported and local ports are still scanned."""
    calls = {}

    def fake_remote(url, api_key, model="", timeout=5.0):
        calls["args"] = (url, api_key, model)
        return {
            "backend": "regolo",
            "url": url,
            "model": "gpt-4o",
            "status": "ok",
        }

    monkeypatch.setattr(llm_probe, "probe_remote", fake_remote)
    monkeypatch.setattr(llm_probe, "_probe_url", lambda url, timeout=3.0: None)
    monkeypatch.setattr(llm_probe, "_probe_text_url", lambda url, timeout=3.0: None)

    result = llm_probe.probe(
        remote_url="https://api.regolo.ai/v1",
        remote_api_key="k",
        remote_model="pinned",
    )

    assert calls["args"] == ("https://api.regolo.ai/v1", "k", "pinned")
    assert any(b["backend"] == "regolo" for b in result["backends"])
    assert "regolo" in result["detected_backends"]


def test_probe_ignores_remote_without_url(monkeypatch):
    """No remote configured -> pure local probing, probe_remote not called."""
    import sentinel.llm_probe as lp

    called = []
    orig_remote = lp.probe_remote
    orig_probe_url = lp._probe_url
    orig_probe_text = lp._probe_text_url

    def tracking_remote(url, api_key, model="", timeout=5.0):
        called.append(url)
        return None

    lp.probe_remote = tracking_remote
    monkeypatch.setattr(lp, "_probe_url", lambda url, timeout=3.0: None)
    monkeypatch.setattr(lp, "_probe_text_url", lambda url, timeout=3.0: None)
    try:
        result = lp.probe()
    finally:
        lp.probe_remote = orig_remote
        monkeypatch.undo()

    assert result == {"backends": [], "detected_backends": []}
    assert called == []
