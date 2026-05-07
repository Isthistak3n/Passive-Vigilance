# Passive Vigilance — Claude Code Context

## Project Overview

Passive Vigilance is a passive RF/WiFi/BT/ADS-B sensor platform running on a Raspberry Pi.
It uses RTL-SDR or HackRF, Kismet, dump1090, a GPS dongle, a WiFi dongle in monitor mode,
and a Bluetooth dongle to passively observe the RF environment without transmitting.

---

## Core Capabilities Being Built

- **Drone RF detection** — passive scan of 2.4 / 5.8 GHz bands for drone command link
  signatures; triggers alerts on detection
- **FAA Remote ID detection** — parses ASTM F3411-22a vendor-specific 802.11 beacons
  (OUI FA:0B:BC) received via Kismet; extracts UAS ID, operator position, drone position,
  UA type, and status; fires alerts per unique UAS ID with configurable rate limiting
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
- **Pluggable alert backend** — abstract `AlertBackend` base class; `NtfyBackend`,
  `TelegramBackend`, `DiscordBackend`, and `ConsoleBackend` implementations; swap via
  `ALERT_BACKEND` in `.env`; `AlertFactory.get_backend()` handles fallback
- **Optional web GUI** — Flask dashboard with live Leaflet map, 5 tabs, SSE stream;
  zero overhead when `GUI_ENABLED=false` (default); `gui/server.py` + templates/static

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
| Kismet auth | Cookie auth: `Cookie: KISMET=<token>` | Kismet 2025.09 dropped API key header auth |

---

## Module Map

| File | Class | Responsibility |
|---|---|---|
| `modules/gps.py` | `GPSModule` | gpsd streaming client; position/time backbone |
| `modules/kismet.py` | `KismetModule` | Kismet REST API; async WiFi + BT device polling |
| `modules/dump1090.py` | `ADSBModule` | dump1090 JSON output; aircraft polling |
| `modules/drone_rf.py` | `DroneRFModule` | pyrtlsdr; passive RF scan for drone signatures |
| `modules/ignore_list.py` | `IgnoreList` | MAC/OUI/SSID filter; atomic JSON persistence |
| `modules/mac_utils.py` | — | MAC randomization detection, type classification, device fingerprinting |
| `modules/alerts.py` | `AlertBackend` / `NtfyBackend` / `TelegramBackend` / `DiscordBackend` / `ConsoleBackend` | Pluggable alert engine — ABC + four backends |
| `modules/kml_writer.py` | `KMLWriter` | Pure Python XML; Google Earth KML with color-coded placemarks and track lines |
| `modules/shapefile.py` | `ShapefileWriter` | geopandas/fiona; write WiFi/aircraft/drone detections as .shp + .geojson + .kml |
| `modules/sdr_manager.py` | `SDRManager` | RTL-SDR inventory detection via rtl_test, SDRMode enum resolution |
| `modules/sdr_coordinator.py` | `SDRCoordinator` | asyncio time-share scheduler for single-dongle setups — slices readsb and DroneRF |
| `modules/remote_id.py` | `RemoteIDModule` | Kismet REST API; parses FAA Remote ID (ASTM F3411-22a) from 802.11 vendor IE (OUI FA:0B:BC) |
| `modules/wigle.py` | `WiGLEUploader` | requests; upload Kismet CSV to WiGLE.net at session end |
| `gui/__init__.py` | — | Empty package marker |
| `gui/server.py` | `GUIServer` | Flask in daemon thread; SSE `/stream`; REST `/api/*` |
| `gui/templates/index.html` | — | Dark-theme SPA; 5 tabs; Leaflet map |
| `gui/static/app.js` | — | SSE client; Leaflet markers; tab/filter logic |
| `gui/static/style.css` | — | Dark theme; touch-friendly |
| `main.py` | `PassiveVigilance` | asyncio orchestrator; SIGINT/SIGTERM → clean shutdown |

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
| Dev/test device | Raspberry Pi 3B+ (username: `XXXXXXX`) at 192.168.1.8 |
| Production target | Raspberry Pi 4B (username: `XXXXX`) at 192.168.1.9 |
| OS (dev Pi) | **Debian 13 Trixie** (not Bookworm, not Raspberry Pi OS) |
| GPS device (Pi3) | `/dev/ttyUSB0` (default) |
| GPS device (Pi4) | `/dev/ttyAMA0` — Waveshare SX126X LoRaWAN/GNSS HAT (L76K GNSS) |
| Kismet port | `2501` |
| dump1090/readsb port | `30003` (SBS-1), `8080` (JSON) |

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

## WiFi Dongle

| Item | Value |
|---|---|
| Device | MediaTek MT7610U (`0e8d:7610`) |
| Interface | `wlan1` |
| Driver | `mt76` series — **in-kernel, no DKMS needed** |
| Mode | monitor (set via udev rule at boot) |
| NM state | unmanaged (`/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf`) |
| udev rule | `/etc/udev/rules.d/99-wifi-monitor.rules` |
| Monitor script | `/usr/local/bin/set-monitor-mode.sh` |

**`wlan0` = Pi built-in WiFi — used for SSH/network. DO NOT set to monitor mode.**
**`wlan1` = USB dongle — monitor mode only. DO NOT use for network connectivity.**

After `sudo systemctl restart NetworkManager`, NM resets wlan1 to managed once.
Re-run the monitor mode commands after any NM restart.

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
- `DRONE_RF_REST_SECONDS` (default `20`) — seconds to sleep after each full frequency sweep; set `0` to disable (falls back to `asyncio.sleep(0.1)`)
- `DRONE_RF_MAX_TEMP_C` (default `75`) — CPU temperature threshold (°C) that doubles the rest period; read from `/sys/class/thermal/thermal_zone0/temp` (millidegrees → Celsius) via `_check_cpu_temp()`; returns `None` if file unavailable

## Kismet Integration

- Kismet runs as a **systemd service** (`deploy/kismet.service`) on boot
- Auth method: **Cookie** — `Cookie: KISMET=<token>` on every REST call. Kismet 2025.09 dropped header-based API key auth.
- Token is generated once via the web UI: http://\<pi-ip\>:2501 → Settings → API Keys
- Kismet logs WiGLE CSV files to the home directory (`~/Kismet-*.wiglecsv`)
- `KismetModule` accepts a `GPSModule` instance and stamps every device record
- `KismetModule` accepts an optional `IgnoreList` instance; ignored devices are silently
  filtered in `poll_devices()` before the list is returned

## Persistence Engine

- `modules/persistence.py` — `PersistenceEngine` class + `DetectionEvent` dataclass
- `modules/probe_analyzer.py` — `ProbeAnalyzer` class (WiFi probe pattern analysis)
- Four time windows: 5 / 10 / 15 / 20 minutes (configurable via `window_minutes`)
- Scoring weights: temporal 35%, location 35%, frequency 20%, signal 10%
- Alert threshold default: 0.7 (configurable via `PERSISTENCE_ALERT_THRESHOLD` in `.env`)
- Alert levels: `suspicious` (0.5–0.7), `likely` (0.7–0.9), `high` (0.9+)
- Location clustering: 100 m threshold, haversine distance, greedy centroid assignment
- Signal normalisation: −85 dBm → 0.0, −40 dBm → 1.0
- Minimum 2 observations required before any score is assigned (prevents first-seen false positives)
- GPS location gate: requires `PERSISTENCE_MIN_LOCATIONS` distinct clusters (default 2)
  when GPS data is present; bypassed when no GPS observations collected
- `purge_old_observations()` called on every `update()` — max 60 min history by default
- `DetectionEvent` fields: `mac`, `score`, `score_breakdown`, `first_seen`, `last_seen`,
  `locations`, `observation_count`, `manufacturer`, `device_type`, `alert_level`
- `ProbeAnalyzer` flags: devices probing > 10 unique SSIDs, or probing surveillance-pattern SSIDs

## KML Output

- `modules/kml_writer.py` — `KMLWriter` class; pure Python, no extra dependencies (stdlib `xml.sax.saxutils`)
- Called automatically from `ShapefileWriter.write_session()` — one call writes shp + geojson + kml
- `write_session(session_id, wifi_events, aircraft_events, drone_events)` → writes `{session_id}/detections.kml`
- `write_session_summary_overlay(session_id, summary)` → inserts ScreenOverlay legend (top-left in Google Earth)
- Three KML Folders: "WiFi/BT Detections", "Aircraft", "Drone RF"
- WiFi placemarks color-coded by alert level: white=new, yellow=suspicious, orange=likely, red=high
- Track LineStrings added for WiFi devices with `locations` list of 2+ GPS clusters; color matches alert level
- Aircraft placed at actual altitude (feet → metres in KML Point coordinates)
- Emergency aircraft use red pushpin style (`aircraft-emergency`)
- KML descriptions use HTML tables in CDATA blocks — render as formatted tables in Google Earth
- KML icon set: Google Maps pushpin/shape URLs (no hosted assets needed)
- `main.py` event_dict includes `mac_type` and `locations` fields so KML can use them
- Session `summary.json` includes `kml_path` key written at shutdown
- KML path logged in shutdown banner

## GPS Quality Filtering

- `GPS_MIN_QUALITY` env var (default `2d`): `any` = skip all quality filtering; `2d` = require mode ≥ 2 and HDOP check; `3d` = require mode 3 and HDOP check
- `GPS_MAX_HDOP` env var (default `5.0`): reject fixes with HDOP above this value; check is skipped when HDOP is NaN (unavailable from gpsd)
- Both settings are read inside `get_fix()` on every call — no module-level constants — so `patch.dict(os.environ, ...)` in tests works without extra patching
- `GPSModule._last_fix_rejected` instance flag: set True when fix is rejected; cleared and INFO logged ("GPS fix quality improved to {mode} HDOP={hdop}") when next fix passes
- When `GPS_MIN_QUALITY=any`: HDOP filter is skipped entirely; `_last_fix_rejected` is reset silently

## MAC Randomization

- `modules/mac_utils.py` — pure utility module, no external dependencies
- `is_randomized_mac(mac)` — returns True if locally administered bit is set (second hex digit of first octet is 2, 6, A, or E)
- `get_mac_type(mac)` — returns `"randomized"` or `"static"`
- `get_randomization_vendor_hint(mac)` — returns `""` for static MACs, `"Unknown"` for randomized (platform cannot be reliably identified from MAC alone)
- `normalize_mac(mac)` — lowercase colon-separated; accepts colons, dashes, compact 12-hex form
- `MACFingerprint` dataclass: `canonical_mac`, `all_macs`, `probe_ssids`, `avg_rssi`, `device_count`
- `group_by_fingerprint(devices)` — clusters randomized MACs that share ≥1 probe SSID using union-find; MACs with no probe SSIDs are never merged
- `KismetModule.poll_devices()` stamps every record with `mac_type` and `is_randomized` fields
- `DetectionEvent` carries `mac_type: str = "static"` field; set via `get_mac_type()` in `_make_event()`
- `PersistenceEngine.__init__()` accepts `handle_randomized: bool` (also `HANDLE_MAC_RANDOMIZATION` env var, default True)
- `PersistenceEngine.get_fingerprint_summary()` — returns current `MACFingerprint` list for tracked randomized MACs
- `IgnoreList.ignore_randomized_macs` — property; default False; configurable via constructor or `IGNORE_RANDOMIZED_MACS` env var
- `IgnoreList.is_ignored_randomized(mac)` — returns True when `ignore_randomized_macs=True` and the MAC is randomized
- `IgnoreList.stats()` now includes `ignore_randomized_macs` key
- Alert bodies for persistence events include `MAC type: static/randomized` field

## Web GUI

- `gui/server.py` — `GUIServer` class; Flask app built in `_build_app()`; imported only when `GUI_ENABLED=true`
- `GUI_ENABLED=false` by default — import of `gui.server` never happens unless opt-in; zero overhead
- `GUIServer.__init__(host, port, orchestrator)` — stores back-reference to orchestrator for `/api/status`
- `GUIServer.start()` — launches Flask in a `daemon=True` thread; returns `False` if Flask not installed
- `GUIServer.push_event(event_type, data)` — thread-safe broadcast; updates `_recent_*` caches; drops dead clients
- SSE pattern: one `threading.Queue(maxsize=500)` per client; `threading.Lock` protects client list
- `/stream` sends a `{"type":"heartbeat"}` every 20 s of silence to keep connections alive
- REST endpoints: `/api/status`, `/api/wifi`, `/api/aircraft`, `/api/drone`, `/api/alerts`
- `_MAX_RECENT = 200` — max events per category kept in memory
- `GUIServer.stop()` — sends `None` sentinel to all queues; clients disconnect cleanly on session end
- `main.py` instantiates `GUIServer` in `__init__`, calls `start()` in `startup()`, `stop()` in `shutdown()`
- `push_event("wifi", event_dict)` called after `all_events.append()` in `_poll_kismet`
- `push_event("aircraft", event)` called after `aircraft_detections.append()` in `_poll_adsb`
- `push_event("drone", event_dict)` called after `drone_detections.append()` in `_poll_drone_rf`
- `gui/templates/index.html` — single SPA; Leaflet loaded from CDN unpkg; 5 tabs; dark theme
- `gui/static/app.js` — WiFi deduplication by MAC; Leaflet dark tile layer with CSS invert filter
- `gui/static/style.css` — KML-matched alert colors (red=high, orange=likely, yellow=suspicious)
- `tests/test_gui.py` — 15 unit tests; no Flask server started during tests

## Alert Engine

- `modules/alerts.py` — `AlertBackend` ABC + `NtfyBackend` + `TelegramBackend` + `DiscordBackend` + `ConsoleBackend`
- `AlertFactory.get_backend(name)` reads `ALERT_BACKEND` from `.env`; falls back to `ConsoleBackend` if unconfigured
- `RateLimiter`: in-memory cooldown dict, resets on restart (intentional)
- Default cooldowns: drone 600 s, persistence 300 s, aircraft 60 s (override in `.env`)
- `ConsoleBackend` always configured — use it for testing without external services
- Ntfy is the primary backend: single HTTP POST, no SDK, no account required
- `TelegramBackend` and `DiscordBackend` are fully implemented stubs — fill credentials to activate
- Priority mapping for ntfy: `low` → `low`, `default` → `default`, `high` → `high`, `urgent` → `max`

## Orchestrator

- `main.py` — `PassiveVigilance` class; `asyncio.run(orchestrator.run())` entry point
- Startup: all modules connect with graceful degradation — any module that fails logs a warning and is skipped
- Poll intervals: GPS 1 s (blocking, via `run_in_executor`), readsb 5 s, Kismet 30 s, DroneRF continuous (background task)
- Session output: `data/sessions/{session_id}/` — `summary.json`, `detections_wifi.shp`, `detections_aircraft.shp`, `detections_drone.shp`, `detections.geojson`
- Shutdown sequence on SIGINT/SIGTERM: stop DroneRF → close Kismet/ADSB/GPS → write `summary.json` → write shapefiles → write GeoJSON/KML → WiGLE upload; each step is wrapped in its own try/except so one failure does not prevent the rest
- `_emergency_flush()` — stdlib-only JSONL dump of all in-memory events to `{session_dir}/emergency_dump.jsonl`; called from the `except` block in `run()` before `finally`; uses no geopandas/requests to survive mid-crash
- `TimeoutStopSec=30` in systemd unit allows clean shutdown to complete
- `_SESSION_OUTPUT_DIR` is a module-level constant read at import time — patch `main._SESSION_OUTPUT_DIR` in tests, not env
- `ShapefileWriter` (`modules/shapefile.py`) — geopandas/fiona; installed via `python3-geopandas` apt package
- `WiGLEUploader` (`modules/wigle.py`) — multipart POST to `https://api.wigle.net/api/v2/file/upload`; HTTP Basic auth
- `_health_banner_loop()` — 5th background task; sleeps `HEALTH_BANNER_INTERVAL_SECONDS` (default 300) then calls `_log_health_banner()`; structured INFO log visible in journalctl
- `_log_health_banner()` — emits session ID, uptime, per-sensor health (✓/✗), cumulative stats from `_stats` dict
- `_stats` dict keys: `kismet_devices_seen`, `aircraft_seen`, `drone_detections`, `alerts_sent`, `alerts_rate_limited`, `persistent_detections` — incremented in poll loops
- `_reconnect(module_name)` — async; close() then connect() with up to `MAX_RECONNECT_ATTEMPTS` (default 3) tries, `RECONNECT_INTERVAL_SECONDS` (default 5) sleep between; triggered only on `True→False` health transition (not repeated failures); sets `_sensor_health[name] = True` on success; logs ERROR and returns False on exhaustion; supports "gps", "kismet", "adsb" (GPS methods wrapped in `run_in_executor`)

## Ignore Lists

- `modules/ignore_list.py` — `IgnoreList` class
- Data files: `data/ignore_lists/mac_ignore.json`, `data/ignore_lists/ssid_ignore.json`
- **git-ignored** — never commit personal device data
- MAC normalization: lowercase colon-separated (`aa:bb:cc:dd:ee:ff`)
- OUI matching: first 3 octets; any MAC in the vendor range is ignored
- SSID matching: case-insensitive
- Atomic saves: write to temp file → `os.rename()` — crash-safe
- CLI: `scripts/manage_ignore_list.py` — `--add-mac`, `--add-oui`, `--add-ssid`,
  `--remove-mac`, `--remove-ssid`, `--list`, `--stats`, `--import-kismet`
- `add_from_kismet(devices)` — bulk-add all devices from a `poll_devices()` result

---

## Deploy Directory

| File | Purpose |
|---|---|
| `deploy/install.sh` | One-command installer; auto-detects OS distro |
| `deploy/kismet.service` | Kismet systemd unit |
| `deploy/passive-vigilance.service` | Orchestrator systemd unit |
| `deploy/gpsd.override.conf` | gpsd drop-in to add `-n` flag |
| `deploy/setup.sh` | Interactive .env configurator — prompts for all credentials; --show masks secrets; --reset wipes config |

`install.sh` auto-detects the OS codename via `lsb_release -cs` for the Kismet repo URL,
so it works on both Bookworm (Pi OS) and Trixie (this dev Pi).

**Python environment:** All pip packages install into `/opt/passive-vigilance/venv` with
`--system-site-packages`. This exposes apt-installed GIS packages (`python3-geopandas`,
`python3-fiona`, `python3-gdal`, `python3-numpy`) without rebuilding them from source on ARM.
Convenience symlink: `/usr/local/bin/pv-python` → `venv/bin/python3`.
The systemd service `ExecStart` points to `/opt/passive-vigilance/venv/bin/python3`.

**GPS device:** `install.sh` reads `GPS_DEVICE` from `.env` before writing `/etc/default/gpsd`.
If `.env` does not exist or `GPS_DEVICE` is unset, it defaults to `/dev/ttyUSB0`.

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
- **Single RTL-SDR time-sharing:** `SDRCoordinator` handles one-dongle setups automatically in `SHARED` mode — slices readsb (ADS-B) and DroneRF on a configurable schedule. Set `SDR_MODE=auto` in `.env` (default). `DEDICATED` mode activates automatically when 2+ dongles are detected.
- **NM resets wlan1 on restart:** `sudo systemctl restart NetworkManager` resets wlan1 to managed once before the unmanaged rule applies. Re-run monitor mode commands after any NM restart. The udev rule handles boot/plug-in automatically.

---

## Commit & Release Standards

Every commit, PR, and release must be human-readable first,
technical second. Follow these rules on every branch.

### Commit message format

First line: plain English summary of what improved (max 72 chars)
- Use "Add", "Fix", "Improve", "Extend" not "feat(scope):"
- Write for a contributor reading git log, not a ticket system
- Bad:  "fix(review): version string, tunable poll intervals, GPS timeout 120s"
- Good: "Tunable poll intervals, GPS resilience, sensor health alerts"

Body: bullet list of user-facing improvements
- Lead with what got better for the operator, not implementation details
- Group related changes together
- No "FIX 1... FIX 10" numbering
- Technical details belong here, not in the subject line

Example:
  Tunable poll intervals, GPS resilience, sensor health alerts

  What's better now:
  - Poll intervals (GPS/Kismet/ADS-B/DroneRF) now tunable via .env
  - GPS startup timeout extended to 120s for real-world dongles
  - Sensor health dict emits WARNING on degradation, INFO on recovery
  - Rate limiter writes are now atomic with file locking
  - Alert backends retry with exponential backoff on network failure

### PR titles
- Plain English, outcome-focused
- Bad:  "fix/operational-resilience"
- Good: "Health banner, auto-reconnect, and sensor resilience"

### Release notes
Always use this structure:
1. One-sentence summary of what this release means
2. "What's better now" bullet list in plain English
3. Test count
4. Optional: "Under the hood" section for technical details

### What NOT to do
- Never number fixes "FIX 1, FIX 2..."
- Never lead with scope tags as the human-readable summary
- Never write release notes that read like a debug log
- Never bury the user benefit inside implementation details
