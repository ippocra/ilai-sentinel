"""Reporter CLI — enrollment, probe, collect, daemon."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from reporter.client import ReporterClient
from reporter.config import Config, load_config
from reporter.hardware import collect as hardware_collect, collect_raw
from reporter.llm_probe import probe as probe_llm
from reporter.queue import OfflineQueue

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def _save_token(token: str, path: str) -> None:
    """Save device token with restricted permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        f.write(token)
    os.chmod(p, 0o600)


def _load_token(path: str) -> str:
    """Load device token from file."""
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        return ""


def _get_hostname() -> str:
    return socket.gethostname() or "unknown"


# ── CLI Commands ──


def cmd_enroll(server: str, code: str, config_path: str | None = None) -> None:
    """Enroll this machine with Mothership using an enrollment code."""
    config = load_config(config_path)
    config.server_url = server
    config.device_id = ""  # Will be set by enrollment

    fp = collect_raw()
    hostname = _get_hostname()

    client = ReporterClient(server, "")  # No token for enrollment
    result = client.enroll(
        code=code,
        hostname=hostname,
        reporter_version="0.1.0",
        hardware_fp=fp["fingerprint"],
    )

    if not result or "device_token" not in result:
        print("ERROR: Enrollment failed. Check server and code.", file=sys.stderr)
        sys.exit(1)

    device_id = result["device_token"]
    token = result["device_token"]  # The full token

    # Store credentials
    _save_token(token, config.auth.token_file)
    config.device_id = result.get("device_id", "")

    print(f"✅ Enrolled successfully.")
    print(f"   Device ID: {result.get('device_id', '—')}")
    print(f"   Token stored at: {config.auth.token_file}")
    print(f"   ⚠️  Token will NOT be shown again.")


def cmd_run_once(config_path: str | None = None) -> None:
    """Run a single metrics collection + submit cycle (for testing)."""
    config = load_config(config_path)
    token = _load_token(config.auth.token_file)
    if not token:
        print("ERROR: No device token found. Run 'enroll' first.", file=sys.stderr)
        sys.exit(1)

    client = ReporterClient(config.server_url, token)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)

    # Collect hardware
    snapshot = hardware_collect()
    # Probe LLM
    llm_results = probe_llm(config.llm.ports, config.llm.extra_urls)

    # Build payload
    payload = {
        **snapshot,
        "llm": llm_results,
    }

    # Try to submit
    success = False
    try:
        result = client.submit_metrics([payload])
        if result and result.get("created"):
            print(f"✅ Metrics submitted ({result['created']} snapshot(s))")
            success = True
    except requests.RequestException as exc:
        logger.error("Submit failed: %s", exc)

    if not success:
        queue.add(payload)
        print(f"⚠️  Submission failed — queued locally ({queue.size()} items waiting)")


def cmd_probe_llm(ports=None, urls=None):
    """Probe for active LLM backends and print results."""
    results = probe_llm(ports or [8888, 8000, 30000], urls or [])
    print(json.dumps(results, indent=2, default=str))


def cmd_collect_hardware() -> None:
    """Collect hardware metrics and print results."""
    data = hardware_collect()
    print(json.dumps(data, indent=2, default=str))


def cmd_service_install(config_path: str | None = None) -> None:
    """Install Reporter as a systemd service."""
    print("ℹ️  Service file template:")
    print()
    print("""[Unit]
Description=Ippocra ILAI Reporter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/reporter daemon --config {config}
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/var/lib/ilai-reporter /var/log/ilai-reporter

[Install]
WantedBy=multi-user.target""".format(config=config_path or "/etc/ilai-reporter/reporter.toml"))
    print()
    print("Install steps:")
    print("  1. Copy the service file above to /etc/systemd/system/ilai-reporter.service")
    print("  2. systemctl daemon-reload")
    print("  3. systemctl enable --now ilai-reporter")


def cmd_status(config_path: str | None = None) -> None:
    """Check reporter status."""
    config = load_config(config_path)
    token = _load_token(config.auth.token_file)
    has_token = bool(token)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)

    print("Reporter Status:")
    print(f"  Server: {config.server_url}")
    print(f"  Device ID: {config.device_id or 'not enrolled'}")
    print(f"  Token: {'configured' if has_token else 'NOT configured'}")
    print(f"  Offline queue: {queue.size()} items")
    print(f"  Config: {config_path or '/etc/ilai-reporter/reporter.toml'}")


def cmd_daemon(config_path: str | None = None) -> None:
    """Run the main daemon loop."""
    _setup_logging()
    config = load_config(config_path)
    token = _load_token(config.auth.token_file)

    if not token:
        logger.error("No device token found. Run 'enroll' first.")
        sys.exit(1)

    client = ReporterClient(config.server_url, token)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)

    logger.info("Reporter daemon starting (server=%s, interval=%ds)", config.server_url, config.metrics_interval_seconds)

    while True:
        try:
            # 1. Drain offline queue
            if queue.size() > 0:
                logger.info("Draining %d queued items...", queue.size())
                items = queue.drain()
                for payload in items:
                    try:
                        result = client.submit_metrics([payload])
                        if result and result.get("created"):
                            # Mark as delivered
                            logger.info("Delivered %d queued items", result["created"])
                    except Exception:
                        logger.warning("Failed to deliver queued item")

            # 2. Collect + submit metrics
            snapshot = hardware_collect()
            llm_results = probe_llm(config.llm.ports, config.llm.extra_urls)
            payload = {**snapshot, "llm": llm_results}

            try:
                result = client.submit_metrics([payload])
                if result and result.get("created"):
                    logger.info("Metrics submitted (%d snapshot(s))", result["created"])
                else:
                    queue.add(payload)
                    logger.warning("Metrics submission failed — queued")
            except requests.RequestException as exc:
                queue.add(payload)
                logger.warning("Metrics submit error: %s — queued", exc)

            # 3. Check for backup jobs
            try:
                job_result = client.claim_backup_job()
                if job_result and job_result.get("job"):
                    logger.info("New backup job: %s", job_result["job"]["id"])
                    # Reporter would execute the backup here
                    # For MVP, we just log it
            except requests.RequestException as exc:
                logger.warning("Job poll error: %s", exc)

            # 4. Wait for next cycle
            time.sleep(config.metrics_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Daemon stopped by user")
            break
        except Exception:
            logger.exception("Unexpected error in daemon loop")
            time.sleep(30)


def main() -> None:
    """CLI entry point."""
    import argparse

    _setup_logging()

    parser = argparse.ArgumentParser(prog="reporter", description="Ippocra ILAI Reporter")
    subparsers = parser.add_subparsers(dest="command")

    # enroll
    p_enroll = subparsers.add_parser("enroll", help="Enroll with Mothership")
    p_enroll.add_argument("--server", required=True, help="Mothership URL")
    p_enroll.add_argument("--code", required=True, help="Enrollment code")

    # run-once
    p_once = subparsers.add_parser("run-once", help="Single metrics collection cycle")
    p_once.add_argument("--config", default=None, help="Config file path")

    # probe-llm
    p_probe = subparsers.add_parser("probe-llm", help="Probe for LLM backends")
    p_probe.add_argument("--ports", nargs="+", type=int, default=None)
    p_probe.add_argument("--urls", nargs="+", default=None)

    # collect-hardware
    subparsers.add_parser("collect-hardware", help="Collect hardware metrics")

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Run as daemon")
    p_daemon.add_argument("--config", default=None, help="Config file path")

    # service
    p_service = subparsers.add_parser("service", help="Service management")
    p_service.add_argument("action", choices=["install", "uninstall"])
    p_service.add_argument("--config", default=None, help="Config file path")

    # status
    p_status = subparsers.add_parser("status", help="Check reporter status")
    p_status.add_argument("--config", default=None, help="Config file path")

    args = parser.parse_args()

    if args.command == "enroll":
        cmd_enroll(args.server, args.code)
    elif args.command == "run-once":
        cmd_run_once(args.config)
    elif args.command == "probe-llm":
        cmd_probe_llm(args.ports, args.urls)
    elif args.command == "collect-hardware":
        cmd_collect_hardware()
    elif args.command == "daemon":
        cmd_daemon(args.config)
    elif args.command == "service":
        if args.action == "install":
            cmd_service_install(args.config)
        else:
            print("Uninstall: run `systemctl disable --now ilai-reporter && rm /etc/systemd/system/ilai-reporter.service`")
    elif args.command == "status":
        cmd_status(args.config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
