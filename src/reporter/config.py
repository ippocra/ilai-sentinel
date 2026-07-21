"""Reporter configuration — loaded from /etc/ilai-reporter/reporter.toml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AuthConfig:
    token_file: str = "/var/lib/ilai-reporter/device.token"


@dataclass
class LLMConfig:
    auto_detect: bool = True
    ports: list[int] = field(default_factory=lambda: [8888])
    extra_urls: list[str] = field(default_factory=list)


@dataclass
class QueueConfig:
    path: str = "/var/lib/ilai-reporter/queue.db"
    max_days: int = 14


@dataclass
class BackupConfig:
    workdir: str = "/var/lib/ilai-reporter/backups"


@dataclass
class Config:
    server_url: str = "https://mothership.ippocra.com"
    device_id: str = ""
    metrics_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 60
    job_poll_interval_seconds: int = 60

    auth: AuthConfig = field(default_factory=AuthConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)


def load_config() -> Config:
    """Load config from environment variables or return defaults.

    For production TOML parsing we'd use `tomllib` (Python 3.11+), but
    the MVP focuses on env vars for simplicity and security.
    """
    config = Config()
    config.server_url = os.environ.get("REPORTER_SERVER_URL", config.server_url)
    config.metrics_interval_seconds = int(
        os.environ.get("REPORTER_METRICS_INTERVAL", str(config.metrics_interval_seconds))
    )
    config.heartbeat_interval_seconds = int(
        os.environ.get("REPORTER_HEARTBEAT_INTERVAL", str(config.heartbeat_interval_seconds))
    )
    token = os.environ.get("REPORTER_DEVICE_TOKEN", "")
    if token:
        config.device_id = token
        config.auth.token_file = "/tmp/reporter.token"
    return config
