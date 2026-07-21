# Reporter

Local daemon installed on deployed ILAI units.

Reporter collects local hardware metrics, probes LLM endpoints, tracks active model/backend usage, and reports to **Mothership** at `https://mothership.ippocra.com`.

Repository pair:

- `ippocra/mothership` — central Django + DRF dashboard/API
- `ippocra/reporter` — local ILAI daemon

See `planning/PLAN.md` for the architecture and implementation plan.

## Core principles

- Runs locally on each deployed ILAI.
- Reports to Mothership over HTTPS.
- Authenticates with device enrollment + device token.
- Keeps an offline queue when Mothership is unreachable.
- Executes backup jobs only when issued/authorized by Mothership.

## License

Proprietary — Ippocra
