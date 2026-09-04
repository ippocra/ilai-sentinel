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


def _parse_llama_metrics_cumulative_tokens(metrics: str | None) -> tuple[int, int]:
    """Extract cumulative token counters from Prometheus metrics.

    Returns (prompt_tokens_total, tokens_predicted_total). llama.cpp exposes
    these as monotonic counters; the sentinel diffs them between scrapes to
    compute tokens consumed since the last cycle. Summed across any model
    labels so router-style deployments (one line per model) add up correctly.
    """
    prompt_total = 0
    predicted_total = 0
    if not metrics:
        return 0, 0
    for line in metrics.splitlines():
        if not line or line.startswith("#"):
            continue
        # Strip trailing value; metric name may carry {labels}
        name_part = line.partition("{")[0].strip()
        # Take the last space-separated token as the value
        value_str = line.rsplit(maxsplit=1)[-1]
        try:
            value = float(value_str)
        except ValueError:
            continue
        # For unlabeled lines the name still has the value appended; drop it
        # so the metric-name suffix match works.
        name_only = name_part.split()[0] if name_part.split() else name_part
        # Match both "llamacpp:prompt_tokens_total" and "llamacpp_prompt_tokens_total"
        normalized = name_only.lower().replace(":", "_")
        if normalized.endswith("prompt_tokens_total"):
            prompt_total += value
        elif normalized.endswith("tokens_predicted_total"):
            predicted_total += value
    return int(prompt_total), int(predicted_total)


def _parse_llama_metrics_gauges(metrics: str | None) -> dict[str, float | None]:
    """Extract real-time performance gauges from Prometheus metrics.

    Returns a dict with:
      - generation_tps: average token generation speed (tokens/s) — the headline
        "how fast is inference" number (llamacpp:tokens_predicted_seconds).
      - prompt_tps: prompt/prefill processing speed (tokens/s)
        (llamacpp:prompt_tokens_seconds).
      - requests_processing: number of client requests being processed now.
      - requests_deferred: number of requests waiting to start.
      - n_tokens_max: largest context-window token count seen.

    Gauges are instantaneous (not cumulative), so we take the max across
    model labels for router deployments rather than summing them.
    """
    result: dict[str, float | None] = {
        "generation_tps": None,
        "prompt_tps": None,
        "requests_processing": None,
        "requests_deferred": None,
        "n_tokens_max": None,
    }
    if not metrics:
        return result

    # metric_name -> list of observed values (across model labels)
    buckets: dict[str, list[float]] = {}
    for line in metrics.splitlines():
        if not line or line.startswith("#"):
            continue
        # Split off any label braces, then strip the trailing numeric value so
        # unlabeled lines (no braces) still yield a clean metric name.
        name_part = line.partition("{")[0].strip()
        name_only = name_part.split()[0] if name_part.split() else name_part
        value_str = line.rsplit(maxsplit=1)[-1]
        try:
            value = float(value_str)
        except ValueError:
            continue
        normalized = name_only.lower().replace(":", "_")
        if normalized.endswith("tokens_predicted_seconds") and not normalized.endswith("total"):
            buckets.setdefault("generation_tps", []).append(value)
        elif normalized.endswith("prompt_tokens_seconds") and not normalized.endswith("total"):
            buckets.setdefault("prompt_tps", []).append(value)
        elif normalized.endswith("requests_processing"):
            buckets.setdefault("requests_processing", []).append(value)
        elif normalized.endswith("requests_deferred"):
            buckets.setdefault("requests_deferred", []).append(value)
        elif normalized.endswith("n_tokens_max"):
            buckets.setdefault("n_tokens_max", []).append(value)

    for key, values in buckets.items():
        if values:
            result[key] = max(values)
    return result


def _probe_metrics_text(url: str) -> str | None:
    """Fetch /metrics text, falling back to per-model router metrics."""
    metrics = _probe_text_url(f"{url}/metrics")
    if metrics is None:
        models_resp = _probe_url(f"{url}/v1/models")
        models = models_resp.get("data", []) if models_resp else []
        if models:
            model = _select_openai_model(models)
            if model:
                metrics = _probe_text_url(f"{url}/metrics?model={model}")
    return metrics


def _probe_tokens_per_sec(url: str) -> float | None:
    """Read optional Prometheus throughput from any compatible backend."""
    metrics = _probe_metrics_text(url)
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
    is_router = False
    if props:
        # A router-style server (multiple models) doesn't expose a single
        # model_name here; the OpenAI-compatible probe resolves the active
        # model, so defer to it rather than claiming an empty model.
        if str(props.get("role", "")).lower() == "router":
            is_router = True
        model = props.get("model_name", props.get("model", ""))
        if not model:
            model_path = props.get("model_path", "")
            if model_path and model_path != "none":
                model = Path(str(model_path)).name

    # Try /metrics for token stats + performance gauges
    metrics = _probe_metrics_text(url)
    tokens_per_sec = _parse_llama_metrics_tokens_per_sec(metrics)
    prompt_total, predicted_total = _parse_llama_metrics_cumulative_tokens(metrics)
    gauges = _parse_llama_metrics_gauges(metrics)

    if not model and not metrics:
        return None
    # Router with no resolvable single model: let the OpenAI probe handle it.
    if is_router and not model:
        return None

    return {
        "backend": "llama.cpp",
        "url": url,
        "model": model,
        "tokens_per_sec": tokens_per_sec,
        "throughput_status": "reported" if tokens_per_sec is not None else "unavailable",
        "tokens_in_total": prompt_total,
        "tokens_out_total": predicted_total,
        "generation_tps": gauges.get("generation_tps"),
        "prompt_tps": gauges.get("prompt_tps"),
        "requests_processing": gauges.get("requests_processing"),
        "requests_deferred": gauges.get("requests_deferred"),
        "n_tokens_max": gauges.get("n_tokens_max"),
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
    metrics = _probe_metrics_text(url)
    prompt_total, predicted_total = _parse_llama_metrics_cumulative_tokens(metrics)
    gauges = _parse_llama_metrics_gauges(metrics)
    return {
        "backend": "vllm" if "vllm" in str(models_resp).lower() else "openai-compatible",
        "url": url,
        "model": model,
        "tokens_per_sec": tokens_per_sec,
        "throughput_status": "reported" if tokens_per_sec is not None else "unavailable",
        "tokens_in_total": prompt_total,
        "tokens_out_total": predicted_total,
        "generation_tps": gauges.get("generation_tps"),
        "prompt_tps": gauges.get("prompt_tps"),
        "requests_processing": gauges.get("requests_processing"),
        "requests_deferred": gauges.get("requests_deferred"),
        "n_tokens_max": gauges.get("n_tokens_max"),
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


def sum_cumulative_tokens(probe_result: dict[str, Any]) -> tuple[int, int]:
    """Sum cumulative token counters across all detected backends.

    Returns (tokens_in_total, tokens_out_total) for the current scrape. The
    sentinel keeps the previous values and diffs them to report per-cycle
    usage. Counters are monotonic; a reset (server restart) is signalled by a
    negative delta, which callers treat as "unknown" and skip.
    """
    tokens_in = 0
    tokens_out = 0
    for backend in probe_result.get("backends", []):
        tokens_in += int(backend.get("tokens_in_total", 0) or 0)
        tokens_out += int(backend.get("tokens_out_total", 0) or 0)
    return tokens_in, tokens_out


class TokenCounter:
    """Diff cumulative llama.cpp token counters between probe cycles.

    The llama.cpp Prometheus endpoint exposes monotonic cumulative counters
    (``llamacpp:prompt_tokens_total`` / ``llamacpp:tokens_predicted_total``).
    By remembering the last-scraped values per backend URL, we can report the
    tokens consumed *during* the interval between two cycles — which is what
    the mothership dashboard needs for its token totals.
    """

    def __init__(self) -> None:
        self._last_in: dict[str, int] = {}
        self._last_out: dict[str, int] = {}
        self._primed = False

    def sample(self, probe_result: dict[str, Any]) -> tuple[int, int] | None:
        """Record current counters and return (delta_in, delta_out) since last call.

        Returns None on the very first sample (no baseline yet) so callers can
        avoid emitting a bogus zero/first-cycle session.
        """
        deltas_in = 0
        deltas_out = 0
        for backend in probe_result.get("backends", []):
            url = str(backend.get("url", ""))
            if not url:
                continue
            cur_in = int(backend.get("tokens_in_total", 0) or 0)
            cur_out = int(backend.get("tokens_out_total", 0) or 0)
            prev_in = self._last_in.get(url)
            prev_out = self._last_out.get(url)
            if prev_in is None or prev_out is None:
                # First time we've seen this backend — no baseline yet.
                self._last_in[url] = cur_in
                self._last_out[url] = cur_out
                continue
            delta_in = cur_in - prev_in
            delta_out = cur_out - prev_out
            if delta_in < 0 or delta_out < 0:
                # Counter reset (server restart) — re-baseline, skip this cycle.
                self._last_in[url] = cur_in
                self._last_out[url] = cur_out
                continue
            deltas_in += delta_in
            deltas_out += delta_out
            self._last_in[url] = cur_in
            self._last_out[url] = cur_out

        if not self._primed:
            self._primed = True
            # First successful sample: establish baseline, report no delta yet.
            return (0, 0)

        return (deltas_in, deltas_out)

    def reset(self) -> None:
        self._last_in.clear()
        self._last_out.clear()
        self._primed = False
