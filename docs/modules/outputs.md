# Outputs (Alerts, GIS, WiGLE, GUI)

Everything the orchestrator decides is interesting is turned into operator-facing output: push alerts, durable session files, GIS layers and a live web dashboard.

## Alerts (`modules/alerts.py`)

Pluggable backends selected by `ALERT_BACKEND`:

| Backend | Transport | Notes |
|---|---|---|
| `NtfyBackend` | HTTP POST | Primary; no account required |
| `TelegramBackend` | Bot API | Credentialed |
| `DiscordBackend` | Webhook | Credentialed |
| `ConsoleBackend` | journal / stdout | Always available; used for health alerts |

A process-wide `RateLimiter` (persisted to `data/rate_limits.json`) enforces per-key cooldowns. Alert dispatch runs on a dedicated single-thread executor so a slow or unreachable backend cannot starve the asyncio loop or the systemd watchdog. The orchestrator also writes every sent alert to `alerts.jsonl` and pushes it live to the GUI.

## GIS writers

- **ShapefileWriter** (`modules/shapefile.py`) — geopandas / fiona point features for WiFi, aircraft and drone detections.
- **KMLWriter** (`modules/kml_writer.py`) — pure-Python Google Earth KML with colour-coded placemarks, track LineStrings and a screen-overlay legend. Called automatically from the shapefile path.
- Session directory layout: `data/sessions/<id>/summary.json`, `detections_*.shp`, `detections.geojson`, `detections.kml`, plus per-type JSONL forensic logs.

## WiGLE (`modules/wigle.py`)

At shutdown the most recent Kismet `.wiglecsv` is uploaded via the WiGLE API (HTTP Basic). Optional; skipped when credentials are absent.

## Web GUI (`gui/server.py`)

Flask application started in a daemon thread when `GUI_ENABLED=true`.

- Fixed-node SPA (`index.html` + Leaflet map) vs mobile-node SPA (`mobile.html`, map-less Nearby + Survey tabs).
- Server-Sent Events (`/stream`) for live updates; REST endpoints for status, history and mode toggle.
- Mode toggle (`POST /api/mode`) writes `NODE_MODE` surgically to `.env` (requires `GUI_TOKEN`); a restart is still required because mode is read only at construction.
- Survey endpoints (`/api/tasking`, `/api/survey`) serve the recon-pair when enabled.

Zero overhead when the GUI is disabled — the import never happens.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ALERT_BACKEND` | ntfy | |
| `GUI_ENABLED` | false | |
| `GUI_PORT` | 8080 (chase uses 8088) | Avoids collision with readsb |
| `GUI_TOKEN` | (empty) | Required for mode toggle and survey endpoints |
| `NTFY_TOPIC` / Telegram / Discord credentials | — | |

## Pitfalls

- Alert storms on randomized MACs are prevented by keying the rate limiter on the rotation-stable fingerprint.
- The GUI’s durable history is rebuilt from on-disk JSONL on refresh, not from the in-memory push cache.
- Shapefile / KML write failures are non-fatal; each step is independently guarded so one failure cannot skip the rest of shutdown.

## Related modules

- [SensorOrchestrator](orchestrator.md) — sole producer of events and the only caller of the writers / backends.
- [docs/setup.md](../setup.md) — full configuration reference.

## Hardware notes

Both development nodes run the GUI on port 8088. **chase** serves the full Leaflet dashboard; **survkis** serves the mobile Nearby + Survey UI.
