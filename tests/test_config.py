# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Sentinel configuration persistence."""

from __future__ import annotations

import stat

from sentinel.config import Config, load_config, save_config


def test_save_config_round_trips_nested_settings(tmp_path):
    config_path = tmp_path / "sentinel.toml"
    config = Config(
        server_url="https://mothership.example.com",
        device_id="device-123",
        metrics_interval_seconds=30,
    )
    config.auth.token_file = str(tmp_path / "device.token")
    config.queue.path = str(tmp_path / "queue.db")
    config.queue.max_days = 7
    config.backup.workdir = str(tmp_path / "backups")
    config.llm.ports = [8888, 8013]
    config.llm.extra_urls = ["http://127.0.0.1:8080"]

    saved_path = save_config(config, config_path)
    loaded = load_config(str(config_path))

    assert saved_path == config_path
    assert loaded.server_url == "https://mothership.example.com"
    assert loaded.device_id == "device-123"
    assert loaded.metrics_interval_seconds == 30
    assert loaded.auth.token_file == str(tmp_path / "device.token")
    assert loaded.queue.path == str(tmp_path / "queue.db")
    assert loaded.queue.max_days == 7
    assert loaded.backup.workdir == str(tmp_path / "backups")
    assert loaded.llm.ports == [8888, 8013]
    assert loaded.llm.extra_urls == ["http://127.0.0.1:8080"]


def test_save_config_restricts_file_and_directory_permissions(tmp_path):
    config_path = tmp_path / "private" / "sentinel.toml"

    save_config(Config(), config_path)

    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
