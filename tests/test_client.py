# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Sentinel HTTP client."""

from __future__ import annotations

from sentinel import __version__
from sentinel.client import SentinelClient


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def test_all_client_post_payloads_include_sentinel_version(monkeypatch):
    posted = []

    def fake_post(self, url, json, headers=None, timeout=None):
        posted.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.Session.post", fake_post)

    client = SentinelClient("https://mothership.example.com", "device-token")

    client.enroll(code="ABCD", hostname="ilai", hermes_version="1.0")
    client.heartbeat(status="online")
    client.submit_metrics([{"cpu_usage": 10}, {"sentinel_version": "custom", "cpu_usage": 20}])
    client.submit_hardware_profile({"gpu_count": 1})
    client.submit_session_event({"event": "started"})
    client.claim_backup_job()
    client.complete_backup_job("job-123", status="success", output="done")

    assert [item["url"] for item in posted] == [
        "https://mothership.example.com/api/enroll/",
        "https://mothership.example.com/api/heartbeat/",
        "https://mothership.example.com/api/metrics/",
        "https://mothership.example.com/api/hardware-profile/",
        "https://mothership.example.com/api/session-event/",
        "https://mothership.example.com/api/jobs/next/",
        "https://mothership.example.com/api/backup-jobs/job-123/complete/",
    ]
    for item in posted:
        # Every POST now carries the Sentinel version under BOTH the
        # legacy `sentinel_version` key and the `reporter_version` key the
        # mothership backend actually persists (contract-drift regression guard).
        assert item["json"]["sentinel_version"] == __version__
        assert item["json"]["reporter_version"] == __version__

    metrics_payload = posted[2]["json"]
    assert metrics_payload["snapshots"] == [
        {
            "sentinel_version": __version__,
            "reporter_version": __version__,
            "cpu_usage": 10,
        },
        {
            "sentinel_version": "custom",
            "reporter_version": "custom",
            "cpu_usage": 20,
        },
    ]