# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""LLM backend probe — detects active LLM endpoints and metadata."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _probe_url(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Try to get metadata from a URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data
    except (json.JSONDecodeError, OSError, TimeoutError, urllib.error.URLError):
        return None


def _probe_text_url(url: str, timeout: float = 3.0) -> str | None:
    """Try to get text from a URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except (OSError, TimeoutError, urllib.error.URLError):
        return None


def _parse_llama_metrics_tokens_per_sec(metrics: str | None) -> float | None:
    """Extract a current token throughput value from Prometheus metrics.

    llama.cpp has used several metric names over time. Prefer explicit
    rate/generation metrics and ignore cumulative token counters.
    """
    if not metrics:
        return None
    candidates: list[tuple[int, float]] = []
    for line in metrics.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, raw_value = line.partition("{")
        if not raw_value:
            name, _, raw_value = line.rpartition(" ")
        try:
            value = float(raw_value.rsplit(maxsplit=1)[-1])
        except ValueError:
            continue
        metric = name.lower()
        if "token" not in metric:
            continue
        if any(marker in metric for marker in ("_total", "_count", "_sum")):
            continue
        priority = 0
        if "predicted_tokens" in metric:
            priority = 3
        elif "tokens_per_second" in metric:
            priority = 2
        elif "tokens_second" in metric:
            priority = 2
        elif "eval_rate" in metric or "generation_rate" in metric or "throughput" in metric or "generation" in metric:
            priority = 1
        elif "second" in metric or "rate" in metric:
            priority = 0
        if priority:
            candidates.append((priority, value))
    return max(candidates, default=(0, None))[1]


def _probe_tokens_per_sec(url: str) -> float | None:
    """Read optional Prometheus throughput from any compatible backend."""
    metrics = _probe_text_url(f"{url}/metrics")
    if metrics is None:
        # Router-style servers may require the active model name for metrics.
        models_resp = _probe_url(f"{url}/v1/models")
        models = models_resp.get("data", []) if models_resp else []
        if models:
            model = _select_openai_model(models)
            if model:
                metrics = _probe_text_url(f"{url}/metrics?model={model}")
    return _parse_llama_metrics_tokens_per_sec(metrics)


def _model_identifier(model: dict[str, Any]) -> str:
    """Return the stable model identifier from an OpenAI-compatible model entry."""
    return str(model.get("id") or model.get("model") or "unknown")


def _is_loaded_openai_model(model: dict[str, Any]) -> bool:
    """Return True when a model entry explicitly represents the loaded model."""
    status = model.get("status")
    if isinstance(status, dict) and str(status.get("value", "")).lower() == "loaded":
        return True
    if isinstance(status, str) and status.lower() == "loaded":
        return True
    return bool(model.get("loaded") or model.get("is_loaded"))


def _select_openai_model(models: list[dict[str, Any]]) -> str:
    """Select the active model from /v1/models, falling back to the first entry."""
    for model in models:
        if _is_loaded_openai_model(model):
            return _model_identifier(model)
    return _model_identifier(models[0])


def probe_llama_cpp(url: str = "http://127.0.0.1:8888") -> dict[str, Any] | None:
    """Probe llama.cpp server for model info."""
    # Try /props for model metadata
    props = _probe_url(f"{url}/props")
    model = ""
    if props:
        model = props.get("model_name", props.get("model", ""))
        if not model:
            model_path = props.get("model_path", "")
            if model_path and model_path != "none":
                model = Path(str(model_path)).name

    # Try /metrics for token stats
    metrics = _probe_text_url(f"{url}/metrics")
    tokens_per_sec = _parse_llama_metrics_tokens_per_sec(metrics)

    if not model and not metrics:
        return None

    return {
        "backend": "llama.cpp",
        "url": url,
        "model": model,
        "tokens_per_sec": tokens_per_sec,
        "throughput_status": "reported" if tokens_per_sec is not None else "unavailable",
        "slots": [],
    }


def probe_vllm_or_openai(url: str = "http://127.0.0.1:8000") -> dict[str, Any] | None:
    """Probe vLLM or OpenAI-compatible server for model info."""
    models_resp = _probe_url(f"{url}/v1/models")
    if not models_resp:
        return None

    models = models_resp.get("data", [])
    if not models:
        return None

    model = _select_openai_model(models)
    tokens_per_sec = _probe_tokens_per_sec(url)
    return {
        "backend": "vllm" if "vllm" in str(models_resp).lower() else "openai-compatible",
        "url": url,
        "model": model,
        "tokens_per_sec": tokens_per_sec,
        "throughput_status": "reported" if tokens_per_sec is not None else "unavailable",
        "slots": [],
    }


def probe_sglang(url: str = "http://127.0.0.1:30000") -> dict[str, Any] | None:
    """Probe sglang server for model info."""
    info = _probe_url(f"{url}/get_server_info")
    if not info:
        return None

    model = info.get("model_names", ["unknown"])[0]
    return {
        "backend": "sglang",
        "url": url,
        "model": model,
        "tokens_per_sec": None,
        "throughput_status": "unavailable",
        "slots": [],
    }


def probe(config_ports: list[int] | None = None, config_urls: list[str] | None = None) -> dict[str, Any]:
    """Probe for active LLM backends across configured ports and URLs.

    Returns:
    {
        "backends": [{"backend": "...", "url": "...", "model": "...", ...}],
        "detected_backends": ["llama.cpp", ...],
    }
    """
    ports = config_ports or [8888, 8013, 8000, 30000]
    urls = config_urls or []

    results: dict[str, Any] = {"backends": [], "detected_backends": set()}

    for port in ports:
        url = f"http://127.0.0.1:{port}"
        # Try each backend type
        for probe_fn in [probe_llama_cpp, probe_vllm_or_openai, probe_sglang]:
            result = probe_fn(url)
            if result:
                results["backends"].append(result)
                results["detected_backends"].add(result["backend"])
                break  # Only one backend per port

    for extra_url in urls:
        for probe_fn in [probe_vllm_or_openai, probe_sglang, probe_llama_cpp]:
            result = probe_fn(extra_url)
            if result:
                results["backends"].append(result)
                results["detected_backends"].add(result["backend"])
                break

    return {
        "backends": results["backends"],
        "detected_backends": sorted(results["detected_backends"]),
    }
