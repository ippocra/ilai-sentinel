# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Sentinel CLI — enrollment, probe, collect, daemon."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from sentinel import __version__
from sentinel.client import SentinelClient
from sentinel.config import DEFAULT_CONFIG_PATH, load_config, save_config
from sentinel.hardware import collect as hardware_collect
from sentinel.hardware import collect_raw
from sentinel.llm_probe import probe as probe_llm
from sentinel.queue import OfflineQueue

logger = logging.getLogger(__name__)


def _setup_logging(log_level: str | None = None) -> None:
    """Configure root logging.

    Priority: explicit level > SENTINEL_LOG_LEVEL env var > INFO.
    """
    effective = log_level or os.environ.get("SENTINEL_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, effective.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


# ── Custom argument types ──


class ServerURL(str):
    """Argparse type that validates http/https scheme."""

    def __new__(cls, value: str) -> ServerURL:
        if not value.startswith(("http://", "https://")):
            raise argparse.ArgumentTypeError(
                f"Server URL must include a scheme (http:// or https://). "
                f"Got: {value}"
            )
        return super().__new__(cls, value)


# ── CLI Commands ──


def cmd_enroll(args: argparse.Namespace) -> None:
    """Enroll this machine with Mothership using an enrollment code."""
    config = load_config(args.config)
    config_path = _config_path_from_args(args)

    config.server_url = args.server
    config.device_id = ""  # Will be set by enrollment

    fp = collect_raw()
    hostname = _get_hostname()

    client = SentinelClient(config.server_url, "")  # No token for enrollment
    result = client.enroll(
        code=args.code,
        hostname=hostname,
        sentinel_version=__version__,
        hermes_version=fp["raw"].get("hermes_version", ""),
    )

    if not result or "device_token" not in result:
        print("ERROR: Enrollment failed. Check server and code.", file=sys.stderr)
        sys.exit(1)

    token = result["device_token"]

    # Store credentials
    _save_token(token, config.auth.token_file)
    config.device_id = result.get("device_id", "")

    # Persist config to disk
    try:
        saved_path = save_config(config, config_path)
        print(f"✅ Enrolled successfully.")
        print(f"   Device ID: {result.get('device_id', '—')}")
        print(f"   Token stored at: {config.auth.token_file}")
        print(f"   Config saved at: {saved_path}")
        print(f"   ⚠️  Token will NOT be shown again.")
    except Exception as exc:
        print(f"✅ Enrolled successfully.")
        print(f"   Device ID: {result.get('device_id', '—')}")
        print(f"   Token stored at: {config.auth.token_file}")
        print(f"   ⚠️  Token will NOT be shown again.")
        print(f"   ⚠️  Warning: could not save config: {exc}")
        print(f"   Set SENTINEL_SERVER_URL env var to configure server.")


def cmd_run_once(args: argparse.Namespace) -> None:
    """Run a single metrics collection + submit cycle (for testing)."""
    config = load_config(args.config)
    token = _load_token(config.auth.token_file)
    if not token:
        print("ERROR: No device token found. Run 'enroll' first.", file=sys.stderr)
        sys.exit(1)

    client = SentinelClient(config.server_url, token)
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


def cmd_probe_llm(args: argparse.Namespace) -> None:
    """Probe for active LLM backends and print results."""
    config = load_config(args.config)
    ports = args.ports if args.ports else config.llm.ports
    urls = args.urls if args.urls else config.llm.extra_urls
    results = probe_llm(ports, urls)
    print(json.dumps(results, indent=2, default=str))


def cmd_collect_hardware(args: argparse.Namespace) -> None:
    """Collect hardware metrics and print results."""
    data = hardware_collect()
    print(json.dumps(data, indent=2, default=str))


def cmd_service(args: argparse.Namespace) -> None:
    """Service management."""
    config_file = str(_config_path_from_args(args))
    if args.action == "install":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_path = unit_dir / "ilai-sentinel.service"
        unit = _service_unit(config_file)

        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(unit)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        except OSError as exc:
            print(f"ERROR: Could not install user service: {exc}", file=sys.stderr)
            sys.exit(1)
        except subprocess.CalledProcessError as exc:
            print(
                f"ERROR: systemctl --user daemon-reload failed with exit code {exc.returncode}",
                file=sys.stderr,
            )
            sys.exit(exc.returncode or 1)

        print(f"✅ Installed user service at {unit_path}")
        print("✅ Reloaded user systemd daemon")

        answer = input("Enable and start ilai-sentinel now? [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            try:
                subprocess.run(
                    ["systemctl", "--user", "enable", "--now", "ilai-sentinel"],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                print(
                    "ERROR: systemctl --user enable --now ilai-sentinel failed "
                    f"with exit code {exc.returncode}",
                    file=sys.stderr,
                )
                sys.exit(exc.returncode or 1)
            print("✅ Enabled and started ilai-sentinel")
        else:
            print("Skipped enable/start. Run this later if needed:")
            print("  systemctl --user enable --now ilai-sentinel")
    elif args.action == "uninstall":
        print(
            "Uninstall: run "
            "`systemctl --user disable --now ilai-sentinel && "
            "rm ~/.config/systemd/user/ilai-sentinel.service && "
            "systemctl --user daemon-reload`"
        )


def cmd_status(args: argparse.Namespace) -> None:
    """Check sentinel status."""
    config = load_config(args.config)
    config_path = _config_path_from_args(args)
    token = _load_token(config.auth.token_file)
    has_token = bool(token)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)

    print("Sentinel Status:")
    print(f"  Server: {config.server_url}")
    print(f"  Device ID: {config.device_id or 'not enrolled'}")
    print(f"  Token: {'configured' if has_token else 'NOT configured'}")
    print(f"  Offline queue: {queue.size()} items")
    print(f"  Config: {config_path}")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Run the main daemon loop."""
    _setup_logging(args.log_level)
    config = load_config(args.config)
    token = _load_token(config.auth.token_file)

    if not token:
        logger.error("No device token found. Run 'enroll' first.")
        sys.exit(1)

    client = SentinelClient(config.server_url, token)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)

    logger.info(
        "Sentinel daemon starting (server=%s, interval=%ds)",
        config.server_url,
        config.metrics_interval_seconds,
    )

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


# ── Helpers ──


def _save_token(token: str, path: str) -> None:
    """Save device token with restricted permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        f.write(token)
    os.chmod(p, 0o600)


def _service_unit(config_file: str) -> str:
    """Return the user-level systemd unit for Sentinel."""
    return f"""[Unit]
Description=Ippocra ILAI Sentinel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/sentinel --config {config_file} daemon
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


def _config_path_from_args(args: argparse.Namespace) -> Path:
    """Return the config path every command should use for this invocation."""
    if args.config:
        return Path(args.config).expanduser()
    return DEFAULT_CONFIG_PATH


def _load_token(path: str) -> str:
    """Load device token from file."""
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        return ""


def _get_hostname() -> str:
    return socket.gethostname() or "unknown"


# ── CLI Parser ──


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    """Add global (non-command) options available to every subcommand."""
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Logging level (default: INFO or SENTINEL_LOG_LEVEL env)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build and return the root argument parser."""
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Ippocra ILAI Sentinel — local monitoring daemon",
        epilog="Use 'sentinel <command> --help' for per-command help.",
    )
    _add_global_options(parser)

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── enroll ──────────────────────────────────────────────────────
    p_enroll = subparsers.add_parser(
        "enroll",
        help="Enroll this machine with Mothership",
        description="Register with Mothership using an enrollment code.",
    )
    p_enroll.add_argument(
        "--server",
        required=True,
        type=ServerURL,
        metavar="URL",
        help="Mothership URL (must start with http:// or https://)",
    )
    p_enroll.add_argument(
        "--code",
        required=True,
        help="Enrollment code",
    )
    p_enroll.set_defaults(func=cmd_enroll)

    # ── run-once ────────────────────────────────────────────────────
    p_once = subparsers.add_parser(
        "run-once",
        help="Run a single metrics collection cycle",
        description="Collect hardware + LLM metrics and submit once (useful for testing).",
    )
    p_once.set_defaults(func=cmd_run_once)

    # ── probe-llm ───────────────────────────────────────────────────
    p_probe = subparsers.add_parser(
        "probe-llm",
        help="Probe for active LLM backends",
        description="Scan for running LLM servers and report what is found.",
    )
    p_probe.add_argument(
        "--ports",
        nargs="+",
        type=int,
        default=None,
        metavar="PORT",
        help="Ports to probe (default: configured llm.ports)",
    )
    p_probe.add_argument(
        "--urls",
        nargs="+",
        default=None,
        metavar="URL",
        help="Extra HTTP URLs to probe",
    )
    p_probe.set_defaults(func=cmd_probe_llm)

    # ── collect-hardware ────────────────────────────────────────────
    subparsers.add_parser(
        "collect-hardware",
        help="Collect hardware metrics",
        description="Gather hardware metrics (CPU, RAM, disk, GPU) and print as JSON.",
    ).set_defaults(func=cmd_collect_hardware)

    # ── daemon ──────────────────────────────────────────────────────
    p_daemon = subparsers.add_parser(
        "daemon",
        help="Run as a background daemon",
        description="Start the Sentinel daemon that collects metrics on a schedule.",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    # ── service ─────────────────────────────────────────────────────
    p_service = subparsers.add_parser(
        "service",
        help="Service management",
        description="Install or manage a user-level systemd service unit.",
    )
    p_service.add_argument(
        "--action",
        choices=["install", "uninstall"],
        required=True,
        help="Service action",
    )
    p_service.set_defaults(func=cmd_service)

    # ── status ──────────────────────────────────────────────────────
    p_status = subparsers.add_parser(
        "status",
        help="Check sentinel status",
        description="Show current enrollment, queue, and config status.",
    )
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    Args:
        argv: Optional list of arguments for testing. Defaults to ``sys.argv[1:]``.
    """
    _setup_logging()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Run the selected command
    args.func(args)


if __name__ == "__main__":
    main()
