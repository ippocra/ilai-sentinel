# Reporter — ILAI Local Reporting Daemon

## Vision

**Reporter** is the local daemon installed on every deployed ILAI unit.

It is responsible for:
- collecting local hardware metrics,
- probing local or cloud-first LLM backends,
- detecting active model/backend/tokens/sec,
- reporting metrics and heartbeats to `https://mothership.ippocra.com`,
- keeping a local offline queue when Mothership is unreachable,
- executing backup jobs only when Mothership issues and authorizes them.

Reporter belongs in its own repository because it has a separate lifecycle from the Mothership web application and must be installed on deployed customer machines.

---

## Relationship with Mothership

```txt
┌──────────────────────────────────────────┐
│ Deployed ILAI                             │
│                                          │
│ ┌──────────────────────────────────────┐ │
│ │ Reporter daemon                       │ │
│ │ - hardware collector                  │ │
│ │ - LLM probe                           │ │
│ │ - offline queue                       │ │
│ │ - backup job executor                 │ │
│ └──────────────────────────────────────┘ │
└─────────────────┬────────────────────────┘
                  │ HTTPS, device auth
                  ▼
┌──────────────────────────────────────────┐
│ Mothership — mothership.ippocra.com       │
│ Django + DRF + PostgreSQL                 │
│ - device enrollment                       │
│ - metrics ingestion                       │
│ - backup orchestration                    │
│ - dashboard                               │
└──────────────────────────────────────────┘
```

Mothership is the source of truth. Reporter does not decide fleet policy. It reports local state and executes Mothership-issued jobs.

---

## Runtime Model

Recommended MVP runtime:
- Python 3.11+ daemon.
- Installed as a `systemd` service.
- Runs as a dedicated Linux user, e.g. `ilai-reporter`.
- Local config under `/etc/ilai-reporter/reporter.toml`.
- Local state/offline queue under `/var/lib/ilai-reporter/`.
- Logs via journald, optional file logs under `/var/log/ilai-reporter/`.

Future alternative:
- Containerized reporter for deployments where Docker/Podman is already standard.

---

## Enrollment / Authentication

### Enrollment command

During provisioning, after creating the ILAI record in Mothership and generating an enrollment code:

```bash
reporter enroll --server https://mothership.ippocra.com --code <ENROLLMENT_CODE>
```

### Flow

1. Reporter sends enrollment code, hostname, reporter version, hardware fingerprint.
2. Mothership validates the short-lived enrollment code.
3. Mothership returns a device ID and one-time device token.
4. Reporter stores token locally with strict permissions.
5. Future requests use device auth.

### MVP auth

```http
Authorization: Bearer <device_token>
```

### Later upgrade path

Design code so we can later replace Bearer tokens with:
- signed requests (`device_id`, timestamp, nonce, HMAC), or
- mTLS device certificates.

---

## Configuration

Example `/etc/ilai-reporter/reporter.toml`:

```toml
server_url = "https://mothership.ippocra.com"
device_id = "..."
metrics_interval_seconds = 60
heartbeat_interval_seconds = 60
job_poll_interval_seconds = 60

[auth]
token_file = "/var/lib/ilai-reporter/device.token"

[llm]
auto_detect = true
ports = [8888]
extra_urls = []

[queue]
path = "/var/lib/ilai-reporter/queue.db"
max_days = 14

[backup]
workdir = "/var/lib/ilai-reporter/backups"
```

---

## Hardware Collector

Reporter should collect:
- CPU usage,
- RAM usage,
- disk usage,
- network RX/TX,
- GPU model/count/utilization/temperature/VRAM if available,
- OS/kernel/hostname,
- raw detected hardware fingerprint.

Implementation notes:
- Use `psutil` for CPU/RAM/disk/network.
- Use `nvidia-smi` when available for NVIDIA GPUs.
- Keep raw payload flexible because ILAI hardware varies.
- If a metric fails, report partial data rather than failing the whole snapshot.

---

## LLM Probe

Reporter probes local or configured LLM endpoints.

### Supported MVP backends

1. **llama.cpp**
   - `/slots` for slot/activity/decode stats
   - `/props` for model metadata when available
2. **vLLM / OpenAI-compatible**
   - `/v1/models` for model list
   - optional Prometheus `/metrics` for token counters if exposed
3. **sglang**
   - `/get_server_info`
   - `/v1/models`
4. **Cloud-first / external model routing**
   - configurable URL(s)
   - reporter records backend/model if exposed by the local proxy/router

### Probe output

```json
{
  "backend": "llama.cpp",
  "model": "Qwen3-30B-A3B-Q4_K_M.gguf",
  "tokens_per_sec": 28.5,
  "ports": [8888],
  "slots": []
}
```

### Behavior

- Timeout quickly (e.g. 3 seconds per endpoint).
- Probe multiple ports if configured.
- Return `unknown` rather than crash if no backend is reachable.
- Detect model changes and include them in reports.

---

## Metrics Report Payload

Reporter posts one snapshot per minute by default.

```json
{
  "timestamp": "2026-07-21T12:00:00Z",
  "reporter_version": "0.1.0",
  "hardware": {
    "cpu_usage": 42.5,
    "ram_usage_gb": 31.2,
    "ram_total_gb": 128,
    "disk_usage_gb": 512,
    "disk_total_gb": 2000,
    "network_rx_mbps": 18.4,
    "network_tx_mbps": 8.2,
    "gpu": [
      {
        "index": 0,
        "model": "RTX 4090",
        "utilization": 76.2,
        "temperature": 71,
        "vram_used_gb": 18.4,
        "vram_total_gb": 24
      }
    ]
  },
  "llm": {
    "backend": "llama.cpp",
    "model": "Qwen3-30B-A3B-Q4_K_M.gguf",
    "tokens_per_sec": 28.5,
    "ports": [8888],
    "slots": []
  }
}
```

Endpoint:

```http
POST /api/reporter/metrics/
Authorization: Bearer <device_token>
```

---

## Offline Queue

Reporter must not lose data if Mothership is temporarily unreachable.

MVP design:
- SQLite queue at `/var/lib/ilai-reporter/queue.db`.
- Each metrics payload is inserted before send or immediately after collection.
- On successful POST, mark as delivered/delete.
- Retry with exponential backoff.
- Keep a configurable max queue age (e.g. 14 days).

---

## Backup Job Execution

### Principle

Backup must always pass through Mothership.

Reporter does not independently schedule backups. It polls Mothership for jobs and executes only authorized jobs.

### Flow

```txt
1. Reporter calls GET /api/reporter/jobs/next/
2. If Mothership has a job, Reporter claims it
3. Reporter receives backup instructions and upload authorization
4. Reporter creates archive locally
5. Reporter uploads using Mothership-approved method
6. Reporter reports object key, size_bytes, checksum, status
7. Mothership stores BackupLog and updates storage usage
```

### Upload strategies

Preferred:
- Mothership returns a short-lived pre-signed DO Spaces upload URL.
- Reporter uploads directly to DO Spaces.
- Reporter never stores global DO Spaces credentials.

MVP alternative:
- Reporter uploads archive to Mothership API.
- Mothership streams it to DO Spaces.

---

## CLI Commands

Target commands:

```bash
reporter enroll --server https://mothership.ippocra.com --code <code>
reporter run-once
reporter probe-llm
reporter collect-hardware
reporter service install
reporter service uninstall
reporter status
```

---

## Systemd Service

Target unit:

```ini
[Unit]
Description=Ippocra ILAI Reporter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ilai-reporter
Group=ilai-reporter
ExecStart=/usr/local/bin/reporter daemon --config /etc/ilai-reporter/reporter.toml
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/var/lib/ilai-reporter /var/log/ilai-reporter

[Install]
WantedBy=multi-user.target
```

---

## Phased Implementation

### Phase 1 — Skeleton
- Python package scaffold.
- CLI with `typer` or `click`.
- Config loader.
- HTTP client.
- Basic logging.

### Phase 2 — Enrollment + auth
- `reporter enroll` command.
- Local token storage.
- Authenticated heartbeat.

### Phase 3 — Hardware + LLM probes
- `psutil` hardware collector.
- `nvidia-smi` GPU collector.
- llama.cpp/vLLM/sglang probes.
- `run-once` verification command.

### Phase 4 — Daemon + offline queue
- Main daemon loop.
- SQLite queue.
- Backoff/retry.
- systemd install helper.

### Phase 5 — Backup executor
- Job polling.
- Job claim/status update.
- Archive creation.
- Pre-signed upload support.
- Backup complete reporting with `size_bytes` and checksum.

### Phase 6 — Packaging
- Install script for provisioning.
- Release artifact.
- Upgrade procedure.

---

## Open Questions

1. Which local config paths should be backed up by default for each ILAI deployment?
2. Do we need model-file backup, or only config/session metadata?
3. Should Reporter auto-discover LLM ports, or rely on Mothership-provided config?
4. Should cloud-first deployments report via local router/proxy metadata, or direct cloud API usage logs?
