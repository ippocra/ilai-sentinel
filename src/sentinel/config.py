# SPDX-FileCopyrightText: 2024 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Sentinel configuration — loaded from environment variables, TOML file, or defaults."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore[assignment]


@dataclass
class AuthConfig:
    token_file: str = os.path.join(Path.home(), ".local/share/ilai-sentinel/device.token")


@dataclass
class LLMConfig:
    auto_detect: bool = True
    ports: list[int] = field(default_factory=lambda: [8888])
    extra_urls: list[str] = field(default_factory=list)


@dataclass
class QueueConfig:
    path: str = os.path.join(Path.home(), ".local/share/ilai-sentinel/queue.db")
    max_days: int = 14


@dataclass
class BackupConfig:
    workdir: str = os.path.join(Path.home(), ".local/share/ilai-sentinel/backups")


@dataclass
class Config:
    server_url: str = ""
    device_id: str = ""
    metrics_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 60
    job_poll_interval_seconds: int = 60

    auth: AuthConfig = field(default_factory=AuthConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ilai-sentinel"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "sentinel.toml"


def _ensure_config_dir(path: Path) -> None:
    """Create config directory with restricted permissions."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


def save_config(config: Config, path: Path | None = None) -> Path:
    """Persist config to a TOML file.

    Returns the path where the config was saved.
    """
    target = path or DEFAULT_CONFIG_PATH
    _ensure_config_dir(target.parent)
    if tomli_w is None:
        raise RuntimeError("tomli-w is required to save config")
    data = asdict(config)
    # Convert nested dataclasses to dicts recursively
    for key in ("auth", "llm", "queue", "backup"):
        data[key] = asdict(data[key])
    with open(target, "w") as f:
        tomli_w.dump(data, f)
    os.chmod(str(target), 0o600)
    return target


def _load_toml(path: Path) -> dict:
    """Load config from a TOML file, returning empty dict if missing."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def load_config(config_path: str | None = None) -> Config:
    """Load config from env vars, TOML file, or return defaults.

    Priority: environment variables > TOML file > defaults.
    """
    config = Config()

    # Try TOML file first
    if config_path:
        toml_path = Path(config_path)
    else:
        toml_path = DEFAULT_CONFIG_PATH

    toml_data = _load_toml(toml_path)

    # Apply TOML values
    if "server_url" in toml_data:
        config.server_url = toml_data["server_url"]
    if "device_id" in toml_data:
        config.device_id = toml_data["device_id"]
    if "metrics_interval_seconds" in toml_data:
        config.metrics_interval_seconds = int(toml_data["metrics_interval_seconds"])
    if "heartbeat_interval_seconds" in toml_data:
        config.heartbeat_interval_seconds = int(toml_data["heartbeat_interval_seconds"])
    if "job_poll_interval_seconds" in toml_data:
        config.job_poll_interval_seconds = int(toml_data["job_poll_interval_seconds"])

    # Apply auth from TOML
    if "auth" in toml_data and isinstance(toml_data["auth"], dict):
        if "token_file" in toml_data["auth"]:
            config.auth.token_file = toml_data["auth"]["token_file"]

    # Apply llm from TOML
    if "llm" in toml_data and isinstance(toml_data["llm"], dict):
        if "auto_detect" in toml_data["llm"]:
            config.llm.auto_detect = toml_data["llm"]["auto_detect"]
        if "ports" in toml_data["llm"]:
            config.llm.ports = [int(p) for p in toml_data["llm"]["ports"]]
        if "extra_urls" in toml_data["llm"]:
            config.llm.extra_urls = toml_data["llm"]["extra_urls"]

    # Apply queue from TOML
    if "queue" in toml_data and isinstance(toml_data["queue"], dict):
        if "path" in toml_data["queue"]:
            config.queue.path = toml_data["queue"]["path"]
        if "max_days" in toml_data["queue"]:
            config.queue.max_days = int(toml_data["queue"]["max_days"])

    # Apply backup from TOML
    if "backup" in toml_data and isinstance(toml_data["backup"], dict):
        if "workdir" in toml_data["backup"]:
            config.backup.workdir = toml_data["backup"]["workdir"]

    # Environment variables override everything
    config.server_url = os.environ.get("SENTINEL_SERVER_URL", config.server_url)
    config.metrics_interval_seconds = int(
        os.environ.get("SENTINEL_METRICS_INTERVAL", str(config.metrics_interval_seconds))
    )
    config.heartbeat_interval_seconds = int(
        os.environ.get("SENTINEL_HEARTBEAT_INTERVAL", str(config.heartbeat_interval_seconds))
    )
    token = os.environ.get("SENTINEL_DEVICE_TOKEN", "")
    if token:
        config.device_id = token
        config.auth.token_file = "/tmp/sentinel.token"
        config.queue.path = "/tmp/sentinel/queue.db"
        config.backup.workdir = "/tmp/sentinel/backups"

    # Test/local override paths
    base_dir = os.environ.get("SENTINEL_DATA_DIR", "")
    if base_dir:
        config.auth.token_file = os.path.join(base_dir, "device.token")
        config.queue.path = os.path.join(base_dir, "queue.db")
        config.backup.workdir = os.path.join(base_dir, "backups")

    return config
