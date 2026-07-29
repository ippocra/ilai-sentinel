# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""HTTP client for communicating with Mothership API."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


class SentinelClient:
    """Thin HTTP client for Mothership endpoints."""

    def __init__(self, server_url: str, device_token: str):
        self.server_url = server_url.rstrip("/")
        self.device_token = device_token
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _token(self) -> str:
        return self.device_token

    def _post(self, path: str, data: dict[str, Any], timeout: int = 30) -> dict[str, Any] | None:
        url = urljoin(self.server_url, path)
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = self.session.post(url, json=data, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Sentinel API POST %s failed: %s", path, exc)
            return None

    def _get(self, path: str, timeout: int = 10) -> dict[str, Any] | None:
        url = urljoin(self.server_url, path)
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = self.session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Sentinel API GET %s failed: %s", path, exc)
            return None

    # ── Enrollment ──

    def enroll(
        self,
        code: str,
        hostname: str,
        sentinel_version: str,
        hermes_version: str = "",
    ) -> dict[str, Any] | None:
        """Exchange enrollment code for device token."""
        url = urljoin(self.server_url, "/api/enroll/")
        try:
            resp = self.session.post(url, json={
                "code": code,
                "hostname": hostname,
                "sentinel_version": sentinel_version,
                "hermes_version": hermes_version,
            }, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Enrollment failed: %s", exc)
            return None

    # ── Sentinel endpoints ──

    def heartbeat(
        self,
        status: str = "online",
        sentinel_version: str = "",
        hermes_version: str = "",
        llm_info: dict | None = None,
    ) -> dict | None:
        return self._post("/api/heartbeat/", {
            "status": status,
            "sentinel_version": sentinel_version,
            "hermes_version": hermes_version,
            "llm": llm_info,
        })

    def submit_metrics(self, snapshots: list[dict]) -> dict | None:
        return self._post("/api/metrics/", {"snapshots": snapshots})

    def submit_hardware_profile(self, profile: dict) -> dict | None:
        return self._post("/api/hardware-profile/", {"profile": profile})

    def submit_session_event(self, event: dict) -> dict | None:
        return self._post("/api/session-event/", {"event": event})

    def claim_backup_job(self) -> dict | None:
        return self._post("/api/jobs/next/", {})

    def complete_backup_job(self, job_id: str, status: str = "success", **kwargs) -> dict | None:
        data = {"status": status, **kwargs}
        return self._post(f"/api/backup-jobs/{job_id}/complete/", data)
