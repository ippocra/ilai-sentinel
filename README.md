# Sentinel — ILAI Local Reporting Daemon

Sentinel is the local daemon installed on every deployed ILAI unit. It collects hardware metrics, probes LLM backends, and reports data to the Mothership fleet manager.

## Quick Start

```bash
# Install
pip install .

# Enroll with Mothership
sentinel enroll --server https://mothership.example.com --code ABCD-1234

# Test a single collection cycle
sentinel run-once

# Check status
sentinel status

# Run as daemon
sentinel daemon

# Install as systemd service
sentinel service install
```

## Configuration

Environment variables (for MVP):

| Variable | Default | Description |
|---|---|---|
| `SENTINEL_SERVER_URL` | *(required)* | Mothership API URL |
| `SENTINEL_METRICS_INTERVAL` | `60` | Seconds between metric submissions |
| `SENTINEL_HEARTBEAT_INTERVAL` | `60` | Seconds between heartbeats |
| `SENTINEL_DEVICE_TOKEN` | | Device token (set after enrollment) |

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
