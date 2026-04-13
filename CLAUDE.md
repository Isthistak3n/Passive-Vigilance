# Passive Vigilance — Claude Code Context

## Project Overview

Passive Vigilance is a passive RF/WiFi/BT/ADS-B sensor platform running on a Raspberry Pi.
It uses RTL-SDR or HackRF, Kismet, dump1090, a GPS dongle, a WiFi dongle in monitor mode,
and a Bluetooth dongle to passively observe the RF environment without transmitting.

---

## Core Capabilities Being Built

- **Drone RF detection** — passive scan of 2.4 / 5.8 GHz bands for drone command link
  signatures; triggers alerts on detection
- **WiFi and Bluetooth device tracking** — Kismet captures and correlates devices; devices
  are logged and geo-stamped
- **ADS-B aircraft detection** — dump1090 decodes Mode S transponders; flights enriched via
  the adsb.lol API (ADSBexchange-compatible format, free tier)
- **GPS-stamped detections** — every sensor event carries lat, lon, altitude, and UTC from
  the GPS module
- **Shapefile output** — detections written as point features to `.shp` for GIS analysis
  (QGIS, ArcGIS, etc.)
- **WiGLE wardriving upload** — at session end, Kismet's native CSV export is uploaded to
  WiGLE.net via their API
- **Pluggable alert backend** — abstract `AlertBackend` base class; `NtfyBackend` is the
  first implementation; Signal and Telegram backends are planned but not yet decided

---

## Architecture Decisions

| Decision | Choice | Reason |
|---|---|---|
| Orchestrator | Python asyncio | Non-blocking polling of multiple slow I/O sources |
| WiFi + BT capture | Kismet (REST API) | Kismet handles monitor mode, deauth avoidance, BTLE |
| ADS-B source | dump1090 JSON output | Lowest-latency local decode |
| ADS-B enrichment | adsb.lol API | Free, ADSBexchange-compatible |
| GPS backbone | gpsd + python3-gps | Every event must carry lat/lon/UTC |
| Alert pluggability | `AlertBackend` ABC | Swap backends without touching orchestrator |
| Event storage | SQLite | Lightweight, no server, query-friendly |
| GIS output | geopandas + fiona | Shapefile write from Python dicts |
| WiGLE upload | Kismet CSV export | Kismet already produces WiGLE-format CSV |

---

## Module Map

| File | Class | Responsibility |
|---|---|---|
| `modules/gps.py` | `GPSModule` | gpsd streaming client; position/time backbone |
| `modules/kismet.py` | `KismetModule` | Kismet REST API; WiFi + BT device polling |
| `modules/dump1090.py` | `ADSBModule` | dump1090 JSON output; aircraft polling |
| `modules/drone_rf.py` | `DroneRFModule` | pyrtlsdr; passive RF scan for drone signatures |
| `modules/alerts.py` | `AlertBackend` / `NtfyBackend` | Abstract alert base + ntfy implementation |
| `modules/shapefile.py` | `ShapefileWriter` | geopandas/fiona; append events as point features |
| `modules/wigle.py` | `WiGLEUploader` | requests; upload Kismet CSV to WiGLE.net API |
| `main.py` | — | asyncio orchestrator; loads .env; SIGINT/SIGTERM shutdown |

---

## Branch Strategy

```
feature/* → dev → main
```

- **`main`** — stable, public releases only. No direct commits ever.
- **`dev`** — integration branch. All feature branches merge here via PR.
- **`feature/*`** — one branch per module or capability. Branch off `dev`.
- Current active branch: `feature/gps-daemon`
- PRs are required to merge into `dev`; maintainer merges `dev` → `main` at release time.

---

## Hardware on This Pi

| Item | Value |
|---|---|
| Username | `survkis` |
| GPS dongle | `/dev/ttyUSB0` (default; override with `GPS_DEVICE` in `.env`) |
| Kismet port | `2501` |
| dump1090 port | `30003` |

---

## Coding Conventions

- All modules use `logging.getLogger(__name__)` — no `print()` statements
- All config loaded from `.env` via `python-dotenv` (`load_dotenv()` at module level)
- Every module has a corresponding test file in `tests/test_<module>.py`
- Stub pattern: modules expose `connect()` / `close()` lifecycle methods where applicable
- Type hints on all public methods
- `python3-gps` (system package, `import gps`) is used — not the pip `gpsd-py3` package

---

## What NOT To Do

- Never commit `.env` — it is gitignored; use `.env.example` for slot documentation
- Never commit `data/`, `logs/`, `*.kismet`, `*.db`, `*.shp`, or any `output/` files
- Never use `sudo` inside Python code
- Never hardcode credentials, API keys, or device paths — always read from environment
- Never commit directly to `main`
