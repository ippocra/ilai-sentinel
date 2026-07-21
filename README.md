# Reporter — ILAI Local Reporting Daemon

Reporter is the local daemon installed on every deployed ILAI unit. It collects hardware metrics, probes LLM backends, and reports data to the Mothership fleet manager.

## Quick Start

```bash
# Install
pip install .

# Enroll with Mothership
reporter enroll --server https://mothership.ippocra.com --code ABCD-1234

# Test a single collection cycle
reporter run-once

# Check status
reporter status

# Run as daemon
reporter daemon

# Install as systemd service
reporter service install
```

## Configuration

Environment variables (for MVP):

| Variable | Default | Description |
|---|---|---|
| `REPORTER_SERVER_URL` | `https://mothership.ippocra.com` | Mothership API URL |
| `REPORTER_METRICS_INTERVAL` | `60` | Seconds between metric submissions |
| `REPORTER_HEARTBEAT_INTERVAL` | `60` | Seconds between heartbeats |
| `REPORTER_DEVICE_TOKEN` | | Device token (set after enrollment) |

## API Endpoints

Reporter calls these Mothership endpoints:

- `POST /api/reporter/enroll/` — Enrollment
- `POST /api/reporter/heartbeat/` — Heartbeat
- `POST /api/reporter/metrics/` — Metric submission
- `POST /api/reporter/hardware-profile/` — Hardware profile update
- `POST /api/reporter/session-event/` — Usage session events
- `POST /api/reporter/backup-jobs/claim/` — Claim backup job
- `POST /api/reporter/backup-jobs/{id}/complete/` — Report job completion

## Project Structure

```
reporter/
├── src/reporter/
│   ├── __init__.py      # Package
│   ├── config.py        # Configuration loader
│   ├── client.py        # HTTP client for Mothership
│   ├── hardware.py      # Hardware metrics collector
│   ├── llm_probe.py     # LLM backend detector
│   ├── queue.py         # Offline queue (SQLite)
│   └── cli.py           # CLI entry point + daemon
├── systemd/
│   └── ilai-reporter.service
├── pyproject.toml
└── README.md
```

## Architecture

```
┌──────────────────────────┐
│ Deployed ILAI Unit       │
│                          │
│ ┌──────────────────────┐ │
│ │ Reporter daemon      │ │
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
