# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""LLM backend probe — detects active LLM endpoints and metadata."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _probe_url(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Try to get metadata from a URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data
    except Exception:
        return None


def probe_llama_cpp(url: str = "http://127.0.0.1:8888") -> dict[str, Any] | None:
    """Probe llama.cpp server for model info."""
    # Try /props for model metadata
    props = _probe_url(f"{url}/props")
    if props:
        model = props.get("model_name", props.get("model", ""))
    else:
        model = ""

    # Try /metrics for token stats
    metrics = _probe_url(f"{url}/metrics")
    tokens_per_sec = 0.0
    if metrics:
        for line in metrics:
            if "token_rate" in str(line):
                try:
                    tokens_per_sec = float(str(line).split()[-1])
                except ValueError:
                    pass

    return {
        "backend": "llama.cpp",
        "url": url,
        "model": model,
        "tokens_per_sec": tokens_per_sec,
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

    model = models[0].get("id", models[0].get("model", "unknown"))
    return {
        "backend": "vllm" if "vllm" in str(models_resp).lower() else "openai-compatible",
        "url": url,
        "model": model,
        "tokens_per_sec": 0.0,
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
        "tokens_per_sec": 0.0,
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
    ports = config_ports or [8888, 8000, 30000]
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
        "detected_backends": list(results["detected_backends"]),
    }
