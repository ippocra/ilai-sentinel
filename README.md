# Sentinel — ILAI Local Reporting Daemon

Sentinel is the local daemon installed on every deployed ILAI unit. It collects hardware metrics, probes LLM backends, and reports data to the Mothership fleet manager.

## Quick Start

### Install

**With `uv` (recommended):**

```bash
# CLI tool — available globally after one command
uv tool install git+https://github.com/ippocra/ilai-sentinel.git
```

**Or with a local virtualenv (for development):**

```bash
uv venv .venv && uv pip install -e .
source .venv/bin/activate
```

### Enroll with Mothership

```bash
sentinel enroll --server https://mothership.example.com --code ABCD-1234
```

Enrollment saves the token and TOML config once. By default all commands read the
same file at `~/.config/ilai-sentinel/sentinel.toml`; use `--config /path/to/sentinel.toml`
before the subcommand when you intentionally want a different file.

### Usage

```bash
# Test a single collection cycle
sentinel run-once

# Check status
sentinel status

# Probe configured LLM ports/URLs
sentinel probe-llm

# Run as daemon
sentinel daemon

# Install as a user-level systemd service
sentinel service --action install
```

`sentinel service --action install` creates
`~/.config/systemd/user/ilai-sentinel.service`, writes a unit that runs
`%h/.local/bin/sentinel --config ~/.config/ilai-sentinel/sentinel.toml daemon`,
and reloads the user systemd daemon. It then asks whether to enable and start
the service with `systemctl --user enable --now ilai-sentinel`.

### User service logs

Because Sentinel is installed as a user-level systemd service, use
`systemctl --user` and `journalctl --user` commands:

```bash
# Check whether the user service is running
systemctl --user status ilai-sentinel

# Follow live logs
journalctl --user -u ilai-sentinel -f

# Show recent logs
journalctl --user -u ilai-sentinel -n 100
```

## Update

To update to the latest version:

```bash
uv tool upgrade ilai-sentinel
```

## Configuration

Default TOML config: `~/.config/ilai-sentinel/sentinel.toml`.

Environment variables (for MVP):

| Variable | Default | Description |
|---|---|---|
| `SENTINEL_SERVER_URL` | *(required)* | Mothership API URL |
| `SENTINEL_METRICS_INTERVAL` | `60` | Seconds between metric submissions |
| `SENTINEL_HEARTBEAT_INTERVAL` | `60` | Seconds between heartbeats |
| `SENTINEL_DEVICE_TOKEN` | | Device token (set after enrollment) |

LLM probing reads `[llm].ports` and `[llm].extra_urls` from the same config by
default. The built-in default ports are `8888`, `8013`, `8000`, and `30000`.
`sentinel probe-llm --ports ... --urls ...` can override those values for one
manual probe.

## Architecture

```
┌──────────────────────────┐
│ Deployed ILAI Unit       │
│                          │
│ ┌──────────────────────┐ │
│ │ Sentinel daemon      │ │
│ │ - Hardware collector │ │
│ │ - LLM probe          │ │
│ │ - Offline queue      │ │
│ │ - Backup executor    │ │
│ └──────────────────────┘ │
└──────────┬───────────────┘
           │ HTTPS + Bearer token
           ▼
┌──────────────────────────┐
│ Mothership               │
│ Django + DRF + PostgreSQL│
└──────────────────────────┘
```

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
