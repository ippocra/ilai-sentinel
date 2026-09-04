# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Sentinel CLI — enrollment, probe, collect, daemon."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
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
from sentinel.llm_probe import TokenCounter, probe as probe_llm
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
        unit_path = _install_user_service(config_file)
        print(f"✅ Installed user service at {unit_path}")
        print("✅ Reloaded user systemd daemon")

        enable = args.yes
        if not args.yes and not args.no_enable:
            answer = input("Enable and start ilai-sentinel now? [y/N]: ").strip().lower()
            enable = answer in {"y", "yes"}

        if enable:
            _run_systemctl(["enable", "--now", "ilai-sentinel"])
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


def cmd_updates(args: argparse.Namespace) -> None:
    """Upgrade Sentinel and refresh its user-level service wiring."""
    config_file = str(_config_path_from_args(args))

    if not args.skip_package_upgrade:
        upgrade_command = shlex.split(args.upgrade_command)
        print(f"Updating package: {' '.join(upgrade_command)}")
        _run_command(upgrade_command, "package upgrade")

    sentinel_command = _sentinel_executable()
    service_command = [
        sentinel_command,
        "--config",
        config_file,
        "service",
        "--action",
        "install",
        "--no-enable",
    ]
    print(f"Refreshing user service: {' '.join(service_command)}")
    _run_command(service_command, "user service refresh")

    if args.no_restart:
        print("Skipped service restart. Run this later if needed:")
        print("  systemctl --user restart ilai-sentinel")
    else:
        _run_systemctl(["restart", "ilai-sentinel"])
        print("✅ Restarted ilai-sentinel user service")


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


def cmd_logs(args: argparse.Namespace) -> None:
    """Show or follow user-service logs for Sentinel."""
    command = [
        "journalctl",
        "--user",
        "-u",
        "ilai-sentinel",
        "--no-pager",
        "-n",
        str(args.lines),
    ]
    if args.since:
        command.extend(["--since", args.since])
    if args.follow:
        command.append("-f")

    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"ERROR: could not run journalctl: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(
            "ERROR: could not read Sentinel logs. Try manually:\n"
            "  journalctl --user -u ilai-sentinel -n 100",
            file=sys.stderr,
        )
        sys.exit(result.returncode or 1)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check local Sentinel setup and print actionable diagnostics."""
    config = load_config(args.config)
    config_path = _config_path_from_args(args)
    token = _load_token(config.auth.token_file)
    queue = OfflineQueue(config.queue.path, config.queue.max_days)
    sentinel_bin = shutil.which("sentinel") or str(Path.home() / ".local" / "bin" / "sentinel")
    unit_path = Path.home() / ".config" / "systemd" / "user" / "ilai-sentinel.service"
    service = _systemctl_user_show("ilai-sentinel")

    failures = 0
    warnings = 0

    def report(level: str, message: str) -> None:
        nonlocal failures, warnings
        icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[level]
        print(f"{icon} {message}")
        if level == "fail":
            failures += 1
        elif level == "warn":
            warnings += 1

    print("Sentinel setup check:")
    report("ok" if config_path.exists() else "fail", f"Config file: {config_path}")
    report("ok" if config.server_url else "fail", f"Server URL: {config.server_url or 'missing'}")
    report("ok" if config.device_id else "warn", f"Device ID: {config.device_id or 'not enrolled'}")
    report(
        "ok" if token else "fail",
        f"Device token: {'configured' if token else 'missing'} ({config.auth.token_file})",
    )
    report("ok" if Path(sentinel_bin).exists() else "fail", f"Sentinel executable: {sentinel_bin}")
    report("ok" if unit_path.exists() else "fail", f"User service unit: {unit_path}")

    load_state = service.get("LoadState", "unknown")
    unit_file_state = service.get("UnitFileState", "unknown")
    active_state = service.get("ActiveState", "unknown")
    sub_state = service.get("SubState", "unknown")

    report("ok" if load_state == "loaded" else "fail", f"systemd load state: {load_state}")
    report("ok" if unit_file_state == "enabled" else "fail", f"systemd enable state: {unit_file_state}")
    report(
        "ok" if active_state == "active" else "fail",
        f"systemd active state: {active_state} ({sub_state})",
    )

    queue_size = queue.size()
    report(
        "ok" if queue_size == 0 else "warn",
        f"Offline queue: {queue_size} item(s) waiting at {config.queue.path}",
    )

    if failures or warnings:
        print("\nSuggested next steps:")
    if unit_file_state != "enabled" or active_state != "active":
        print("  systemctl --user enable --now ilai-sentinel")
    if queue_size:
        print("  sentinel logs -n 100")
        print("  sentinel run-once")
    if not token:
        print("  sentinel enroll --server <mothership-url> --code <enrollment-code>")

    sys.exit(1 if failures else 0)


def _maybe_report_token_usage(
    client: "SentinelClient",
    counter: "TokenCounter",
    probe_result: dict,
    logger: logging.Logger,
) -> None:
    """Diff cumulative token counters and report a session event if tokens moved.

    The mothership dashboard's token totals come from UsageSession rows, which
    are only created by the session-event endpoint. llama.cpp exposes
    cumulative token counters; by diffing them between probe cycles we can
    report the tokens consumed during the interval as a single session event.
    """
    delta = counter.sample(probe_result)
    if delta is None:
        return
    tokens_in, tokens_out = delta
    if tokens_in <= 0 and tokens_out <= 0:
        return
    # Report as a completed usage session: the interval's consumed tokens.
    event = {
        "action": "end",
        "model": "",
        "backend": "",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "avg_latency_ms": 0,
        "tags": {"source": "sentinel-token-delta"},
    }
    try:
        result = client.submit_session_event(event)
        if result:
            logger.info(
                "Reported token usage: %d in / %d out", tokens_in, tokens_out
            )
        else:
            logger.warning("Token usage session event was not accepted")
    except Exception as exc:
        logger.warning("Failed to report token usage: %s", exc)


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
    token_counter = TokenCounter()

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
                items = queue.drain_with_ids()
                for item_id, payload in items:
                    try:
                        result = client.submit_metrics([payload])
                        if result and result.get("created"):
                            queue.mark_delivered([item_id])
                            logger.info("Delivered queued item %d", item_id)
                    except Exception:
                        logger.warning("Failed to deliver queued item %d", item_id)

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

            # 3. Report token usage (diff of cumulative counters)
            _maybe_report_token_usage(client, token_counter, llm_results, logger)

            # 4. Check for backup jobs
            try:
                job_result = client.claim_backup_job()
                if job_result and job_result.get("job"):
                    logger.info("New backup job: %s", job_result["job"]["id"])
            except requests.RequestException as exc:
                logger.warning("Job poll error: %s", exc)

            # 5. Wait for next cycle
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


def _install_user_service(config_file: str) -> Path:
    """Write the user-level Sentinel systemd unit and reload user systemd."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / "ilai-sentinel.service"
    unit = _service_unit(config_file)

    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit)
        _run_systemctl(["daemon-reload"])
    except OSError as exc:
        print(f"ERROR: Could not install user service: {exc}", file=sys.stderr)
        sys.exit(1)

    return unit_path


def _run_systemctl(args: list[str]) -> None:
    """Run a user-level systemctl command and exit cleanly on failure."""
    command = ["systemctl", "--user", *args]
    _run_command(command, "systemctl --user " + " ".join(args))


def _systemctl_user_show(unit: str) -> dict[str, str]:
    """Return selected systemd user-service properties, or empty data if unavailable."""
    command = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property",
        "LoadState,ActiveState,SubState,UnitFileState,FragmentPath,ExecMainStatus,NRestarts",
        "--no-pager",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    return properties


def _run_command(command: list[str], label: str) -> None:
    """Run a command and exit cleanly on failure."""
    if not command:
        print(f"ERROR: {label} command is empty", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: {label} failed with exit code {exc.returncode}", file=sys.stderr)
        sys.exit(exc.returncode or 1)
    except OSError as exc:
        print(f"ERROR: {label} could not be started: {exc}", file=sys.stderr)
        sys.exit(1)


def _sentinel_executable() -> str:
    """Return the installed Sentinel executable path used by the user service."""
    user_bin = Path.home() / ".local" / "bin" / "sentinel"
    if user_bin.exists():
        return str(user_bin)
    return "sentinel"


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
    install_mode = p_service.add_mutually_exclusive_group()
    install_mode.add_argument(
        "--yes",
        action="store_true",
        help="With --action install, enable and start without prompting",
    )
    install_mode.add_argument(
        "--no-enable",
        action="store_true",
        help="With --action install, install and reload without enabling or starting",
    )
    p_service.set_defaults(func=cmd_service)

    # ── updates ─────────────────────────────────────────────────────
    p_updates = subparsers.add_parser(
        "updates",
        help="Upgrade Sentinel and refresh the user service",
        description=(
            "Run the package upgrade, reinstall the user systemd service unit, "
            "reload user systemd, and restart the service."
        ),
    )
    p_updates.add_argument(
        "--upgrade-command",
        default="uv tool upgrade ilai-sentinel",
        help="Package upgrade command to run before service refresh",
    )
    p_updates.add_argument(
        "--skip-package-upgrade",
        action="store_true",
        help="Only refresh/restart the user service; do not run the package upgrade command",
    )
    p_updates.add_argument(
        "--no-restart",
        action="store_true",
        help="Refresh the service unit and reload systemd, but do not restart the service",
    )
    p_updates.set_defaults(func=cmd_updates)

    # ── status ──────────────────────────────────────────────────────
    p_status = subparsers.add_parser(
        "status",
        help="Check sentinel status",
        description="Show current enrollment, queue, and config status.",
    )
    p_status.set_defaults(func=cmd_status)

    # ── logs ────────────────────────────────────────────────────────
    p_logs = subparsers.add_parser(
        "logs",
        help="Show Sentinel user-service logs",
        description="Read ilai-sentinel logs from the user-level systemd journal.",
    )
    p_logs.add_argument(
        "-n",
        "--lines",
        type=int,
        default=100,
        help="Number of recent log lines to show (default: 100)",
    )
    p_logs.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow live logs",
    )
    p_logs.add_argument(
        "--since",
        default=None,
        help='Only show logs since this time, e.g. "1 hour ago" or "2026-08-24"',
    )
    p_logs.set_defaults(func=cmd_logs)

    # ── doctor ──────────────────────────────────────────────────────
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Check whether Sentinel is properly set up",
        description="Verify config, token, executable, user service, active state, and queue.",
    )
    p_doctor.set_defaults(func=cmd_doctor)

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
