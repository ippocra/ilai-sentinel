"""Hardware metrics collector — CPU, RAM, disk, network, GPU."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)


def _get_gpu_info() -> list[dict[str, Any]]:
    """Collect GPU info from nvidia-smi if available."""
    gpus: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,model,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 6:
                    gpus.append({
                        "index": int(parts[0]),
                        "model": parts[1],
                        "utilization": float(parts[2]),
                        "temperature": float(parts[3]),
                        "vram_used_gb": round(int(parts[4]) / 1024, 2),
                        "vram_total_gb": round(int(parts[5]) / 1024, 2),
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as exc:
        logger.debug("GPU info collection failed: %s", exc)
    return gpus


def _get_network_throughput() -> tuple[float, float]:
    """Get current network RX/TX in Mbps using psutil counters."""
    if not psutil:
        return (0.0, 0.0)
    try:
        counters = psutil.net_io_counters()
        return (
            round(counters.bytes_recv / (1024 * 1024), 2),
            round(counters.bytes_sent / (1024 * 1024), 2),
        )
    except Exception:
        return (0.0, 0.0)


def collect() -> dict[str, Any]:
    """Collect all available hardware metrics.

    Returns a dict suitable for the Reporter metrics payload:
    {
        "timestamp": "...",
        "hostname": "...",
        "hardware": {...},
        "reporter_version": "0.1.0"
    }
    """
    hostname = platform.node() or os.uname().nodename or "unknown"

    hardware: dict[str, Any] = {
        "hostname": hostname,
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
    }

    if psutil:
        try:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=0.5)
            hardware["cpu_usage"] = cpu_percent
            hardware["cpu_cores_logical"] = psutil.cpu_count(logical=True)
            hardware["cpu_cores_physical"] = psutil.cpu_count(logical=False)

            # RAM
            vm = psutil.virtual_memory()
            hardware["ram_usage_gb"] = round(vm.used / (1024 ** 3), 2)
            hardware["ram_total_gb"] = round(vm.total / (1024 ** 3), 2)
            hardware["ram_percent"] = vm.percent

            # Disk
            disk = psutil.disk_usage("/")
            hardware["disk_usage_gb"] = round(disk.used / (1024 ** 3), 2)
            hardware["disk_total_gb"] = round(disk.total / (1024 ** 3), 2)

            # Network throughput
            rx_mb, tx_mb = _get_network_throughput()
            hardware["network_rx_mb"] = rx_mb
            hardware["network_tx_mb"] = tx_mb
        except Exception as exc:
            logger.warning("psutil collection failed: %s", exc)
    else:
        logger.debug("psutil not available — limited hardware data")

    # GPU
    gpus = _get_gpu_info()
    hardware["gpus"] = gpus
    if gpus:
        hardware["gpu_count"] = len(gpus)
        hardware["gpu_vram_total_gb"] = round(sum(g["vram_total_gb"] for g in gpus), 2)
        hardware["gpu_vram_used_gb"] = round(sum(g["vram_used_gb"] for g in gpus), 2)
        hardware["gpu_temps"] = {g["index"]: g["temperature"] for g in gpus}
    else:
        hardware["gpu_count"] = 0

    hardware["collected_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "hardware": hardware,
        "reporter_version": "0.1.0",
    }


def collect_raw() -> dict[str, Any]:
    """Collect raw hardware fingerprint for enrollment."""
    fingerprint = {
        "hostname": platform.node() or "unknown",
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_cores": psutil.cpu_count(logical=True) if psutil else 0,
        "ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2) if psutil else 0,
        "gpu_count": len(_get_gpu_info()),
    }
    return {
        "fingerprint": json.dumps(fingerprint, sort_keys=True),
        "raw": fingerprint,
    }
