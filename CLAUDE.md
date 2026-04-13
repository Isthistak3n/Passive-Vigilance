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
| Kismet auth | API key (`KISMET-API-Key` header) | More secure than basic auth; generated in web UI |

---

## Module Map

| File | Class | Responsibility |
|---|---|---|
| `modules/gps.py` | `GPSModule` | gpsd streaming client; position/time backbone |
| `modules/kismet.py` | `KismetModule` | Kismet REST API; async WiFi + BT device polling |
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
- PRs are required to merge into `dev`; maintainer merges `dev` → `main` at release time.

---

## Hardware

| Item | Value |
|---|---|
| Dev/test device | Raspberry Pi 3B+ (username: `survkis`) |
| Production target | Raspberry Pi 4B+ (not yet configured) |
| OS (dev Pi) | **Debian 13 Trixie** (not Bookworm, not Raspberry Pi OS) |
| GPS dongle | `/dev/ttyUSB0` (default; override with `GPS_DEVICE` in `.env`) |
| Kismet port | `2501` |
| dump1090 port | `30003` |

---

## Software Versions (dev Pi)

| Software | Version | Install method |
|---|---|---|
| Kismet | `2025.09.0` | apt — `kismet-release` trixie repo |
| Kismet binary | `/usr/bin/kismet` | — |
| readsb | `3.14.1630` | apt (Trixie main) — dump1090-fa drop-in |
| readsb binary | `/usr/bin/readsb` | — |
| gpsd | `3.25` | apt |
| Python | `3.13` | system |
| python3-gps | system package | apt (not pip) |
| pyrtlsdr | `0.2.93` (pinned) | pip — **do not upgrade** |
| librtlsdr | `2.0.2` (Osmocom fork) | apt |

---

## ADS-B / readsb Integration

- `readsb` (not dump1090) is the ADS-B decoder; binary at `/usr/bin/readsb`
- readsb is a drop-in for dump1090-fa — same ports, same JSON format
- JSON aircraft data: `http://localhost:8080/data/aircraft.json`
- SBS-1 stream: `tcp://localhost:30003`
- readsb runs as systemd service, activates automatically when RTL-SDR dongle is connected
- adsb.lol enrichment API: `https://adsbexchange-com1.p.rapidapi.com/v2/icao/{icao}/`
  - Header: `x-rapidapi-key: <ADSBXLOL_API_KEY>`
  - Returns: registration (`r`), type (`t`), operator (`ownOp`), `dbFlags` (bit 0 = military)

## RTL-SDR / Drone RF

- pyrtlsdr pinned to `0.2.93` — **do not upgrade** (librtlsdr 2.0.2 missing `rtlsdr_set_dithering`)
- Known RTL-SDR USB vendor IDs: `0bda:2832`, `0bda:2838`, `0bda:2813`
- Drone scan frequencies: 433 MHz, 868 MHz, 915 MHz, 2.4 GHz, 5.8 GHz
- R820T/R820T2 dongles max ~1750 MHz; 2.4 GHz requires E4000 chip; 5.8 GHz = out of range
- `DroneRFModule` and `readsb` both need an RTL-SDR — two dongles needed to run simultaneously
- Kernel modules to blacklist: `dvb_usb_rtl28xxu`, `rtl2832`, `rtl2830`

## Kismet Integration

- Kismet runs as a **systemd service** (`deploy/kismet.service`) on boot
- Auth method: **API key** — `KISMET-API-Key: <key>` header on every REST call
- API key is generated once via the web UI: http://\<pi-ip\>:2501 → Settings → API Keys
- Kismet logs WiGLE CSV files to the home directory (`~/Kismet-*.wiglecsv`)
- `KismetModule` accepts a `GPSModule` instance and stamps every device record

---

## Deploy Directory

| File | Purpose |
|---|---|
| `deploy/install.sh` | One-command installer; auto-detects OS distro |
| `deploy/kismet.service` | Kismet systemd unit |
| `deploy/passive-vigilance.service` | Orchestrator systemd unit |
| `deploy/gpsd.override.conf` | gpsd drop-in to add `-n` flag |

`install.sh` auto-detects the OS codename via `lsb_release -cs` for the Kismet repo URL,
so it works on both Bookworm (Pi OS) and Trixie (this dev Pi).

---

## Coding Conventions

- All modules use `logging.getLogger(__name__)` — no `print()` statements
- All config loaded from `.env` via `python-dotenv` (`load_dotenv()` at module level)
- Every module has a corresponding test file in `tests/test_<module>.py`
- Stub pattern: modules expose `connect()` / `close()` lifecycle methods where applicable
- Type hints on all public methods
- `python3-gps` (system package, `import gps`) is used — not the pip `gpsd-py3` package
- Kismet module uses `aiohttp` for async REST calls

---

## What NOT To Do

- Never commit `.env` — it is gitignored; use `.env.example` for slot documentation
- Never commit `data/`, `logs/`, `*.kismet`, `*.db`, `*.shp`, or any `output/` files
- Never use `sudo` inside Python code
- Never hardcode credentials, API keys, or device paths — always read from environment
- Never commit directly to `main`

---

## Known Issues / Gotchas

- **Kismet apt install debconf hang:** `apt install kismet` may hang on a debconf dialog
  asking about suid-root helpers. Fix: `echo "kismet-capture-common kismet-common/suid-root boolean true" | sudo debconf-set-selections` then `sudo kill $(pgrep apt) && sudo dpkg --configure -a`
- **Debian Trixie vs Bookworm:** The Kismet repo URL must match the OS codename exactly.
  Using `bookworm` packages on `trixie` fails with `libwebsockets17` dependency errors.
- **`kismet --version` exits non-zero** even on success — don't rely on its exit code in scripts.
- **python3-gps vs gpsd-py3:** System package is `import gps`; pip package is `import gpsd`. They are incompatible. Always use the apt package.
- **pyrtlsdr 0.3.0+ breaks on Trixie:** `librtlsdr 2.0.2` (Osmocom fork) doesn't export `rtlsdr_set_dithering`. Pin stays at `0.2.93`.
- **readsb JSON port:** readsb serves aircraft JSON on port 8080 (HTTP), not 30003 (SBS-1 TCP). The `.env` `DUMP1090_PORT=30003` is the SBS-1 port; the `ADSBModule` connects to port 8080 directly.
- **Single RTL-SDR conflict:** readsb and DroneRFModule can't share one dongle simultaneously. Use two dongles or stop readsb before drone scanning.
