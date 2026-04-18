# Changelog

All notable changes to Passive Vigilance are documented here.

Versions follow the project's alpha release cadence.
Each entry describes what got better for the operator, not implementation detail.

---

## [Unreleased]

- No unreleased changes since v0.4-alpha.

---

## [v0.4-alpha] — 2026-04-18 — 207 tests passing

### Optional web GUI with live map, SSE stream, and dark theme

- New browser dashboard accessible at `http://[pi-ip]:8080` when `GUI_ENABLED=true`
- Live Leaflet map shows WiFi, aircraft, and drone RF detections as they arrive — no page refresh
- Five tabs: Map, WiFi/BT, Aircraft, Drone RF, Alerts — each filterable by any field
- WiFi detections color-coded by alert level (red = high, orange = likely, yellow = suspicious)
- Sensor health bar in the header turns green/red as GPS, Kismet, ADS-B, and DroneRF degrade
- Server-Sent Events stream at `/stream`; REST API at `/api/status`, `/api/wifi`, `/api/aircraft`, `/api/drone`, `/api/alerts`
- Flask runs in a daemon thread — the asyncio sensor loop is never blocked
- `GUI_ENABLED=false` by default: Flask is never imported and adds zero overhead when off
- `flask>=3.0.0` added to `requirements.txt`
- GUI default port changed to `8080` throughout code, docs, and `.env.example`
- GitHub Actions pinned to Node.js 24 native versions (`checkout@v4.2.2`, `setup-python@v5.4.0`, `upload-artifact@v4.6.2`); `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` workaround removed
- 15 new tests covering push_event, SSE broadcast, thread safety, and graceful stop

### Includes all v0.3.x and v0.2.x improvements

---

## [v0.3.1-alpha] — 2026-04-17 — 188 tests passing

### Google Earth KML output

- Every session now produces `detections.kml` alongside shapefiles and GeoJSON automatically
- WiFi/BT placemarks color-coded by alert level: white (new), yellow (suspicious), orange (likely), red (high)
- Track LineStrings drawn for any device seen at two or more distinct GPS clusters — movement patterns visible at a glance in Google Earth
- Aircraft placed at actual altitude (feet → metres) in KML Point coordinates
- Emergency aircraft use a distinct red pushpin style
- KML descriptions render as formatted HTML tables inside Google Earth
- `write_session_summary_overlay()` inserts a ScreenOverlay legend with session stats (WiFi count, aircraft count, drone count, duration)
- No new dependencies — pure Python stdlib (`xml.sax.saxutils`)
- 14 new tests

---

## [v0.3-alpha] — 2026-04-17 — 174 tests passing

### MAC randomization detection and fingerprint grouping

- Randomized MACs (iOS 14+, Android 10+, Windows 10+) are detected automatically via the IEEE 802 locally administered bit
- Every Kismet device record now carries `mac_type` (`static` or `randomized`) and `is_randomized` fields
- `PersistenceEngine` optionally groups randomized MACs that share probe SSIDs into fingerprint clusters — tracks likely-same devices across MAC changes
- `get_fingerprint_summary()` returns current `MACFingerprint` clusters: canonical MAC, all observed MACs, shared probe SSIDs, average RSSI
- `IGNORE_RANDOMIZED_MACS=true` silently drops all randomized MACs from scoring — useful in crowded environments
- Alert bodies now include `MAC type: static/randomized` field
- `DetectionEvent` carries `mac_type` so GIS outputs and KML reflect MAC type
- `HANDLE_MAC_RANDOMIZATION=true` (default) — set false to skip fingerprint grouping
- 14 new tests covering randomization detection, normalization, and fingerprint clustering

---

## [v0.2.2-alpha] — 2026-04-17 — 154 tests passing

### Operational resilience: health banner and auto-reconnect

- 5-minute health banner logged to `journalctl` on schedule — session ID, uptime, per-sensor status (✓/✗), cumulative event and alert counts
- Auto-reconnect on first sensor failure: GPS, Kismet, and ADS-B close and reopen before declaring degraded — handles transient USB resets and network hiccups without a service restart
- Reconnect attempts and interval configurable via `MAX_RECONNECT_ATTEMPTS` and `RECONNECT_INTERVAL_SECONDS`
- Health banner interval configurable via `HEALTH_BANNER_INTERVAL_SECONDS`
- Sensor health transitions emit WARNING on degradation and INFO on recovery
- `_VERSION` string corrected to `0.2.1-alpha`

---

## [v0.2.1-alpha] — 2026-04-17 — 154 tests passing

### Code review and CI hardening

- All four sensor poll intervals now tunable via `.env` (`GPS_POLL_INTERVAL_SECONDS`, `ADSB_POLL_INTERVAL_SECONDS`, `KISMET_POLL_INTERVAL_SECONDS`, `DRONE_POLL_INTERVAL_SECONDS`)
- GPS startup timeout extended to 120 s — gives real-world USB dongles time for cold-start fix
- GPS device path falls back gracefully when `GPS_DEVICE` is unset
- Alert backends retry with exponential backoff on network failure
- Rate limiter writes are atomic (temp file + `os.rename`) — crash-safe
- `numpy` pinned to `>=1.24,<3` to avoid source-tree conflicts in CI
- CI green on Python 3.11 and 3.13 — `librtlsdr0` added, `conftest.py` guards for `python3-gps` path in virtualenv, `drone_rf` numpy import guarded
- GitHub Actions added with pytest matrix and flake8 lint

---

## [v0.2-alpha] — 2026-04-17 — 145 tests passing

### Orchestrator fully wired

- `main.py` orchestrator wires all sensors: Kismet → PersistenceEngine → AlertEngine pipeline active
- Persistent rate limiters survive service restarts — no re-alerting on the same device after a restart
- Incremental JSONL session logging: `events.jsonl`, `aircraft.jsonl`, `drone.jsonl` written on every detection — crash-safe, no data loss if the service is killed mid-session
- WiGLE CSV upload at session end: Kismet's native `.wiglecsv` file is uploaded to WiGLE.net automatically if `WIGLE_API_NAME` and `WIGLE_API_KEY` are set
- Shapefile, GeoJSON, and session `summary.json` written at clean shutdown
- 145 tests passing

---

## [v0.1-alpha] — 2026-04-17 — 139 tests passing

### First complete release — all modules wired

This release marks the first end-to-end working sensor platform. Every module
is implemented, tested, and connected to the orchestrator.

**Sensor modules:**
- `GPSModule` — gpsd streaming client; every detection carries lat, lon, UTC
- `KismetModule` — Kismet REST API with API key auth; WiFi + BT device polling
- `ADSBModule` — readsb JSON output; aircraft polling with adsb.lol enrichment (registration, type, operator, military flag)
- `DroneRFModule` — pyrtlsdr passive scan at 433/868/915 MHz/2.4 GHz for drone command links

**Intelligence:**
- `PersistenceEngine` — time-window scoring across 5/10/15/20 min windows; 0.0–1.0 surveillance confidence score; alert levels: suspicious / likely / high
- `ProbeAnalyzer` — flags devices probing > 10 unique SSIDs or known surveillance-pattern SSIDs
- `IgnoreList` — MAC, OUI prefix, and SSID filtering with CLI management tool (`scripts/manage_ignore_list.py`)

**Output:**
- `ShapefileWriter` — geopandas/fiona; WiFi, aircraft, and drone detections as `.shp` point features plus `.geojson` FeatureCollection
- `WiGLEUploader` — multipart POST of Kismet `.wiglecsv` to WiGLE.net

**Alert engine:**
- `AlertBackend` ABC with `NtfyBackend`, `TelegramBackend`, `DiscordBackend`, `ConsoleBackend`
- `AlertFactory.get_backend()` reads `ALERT_BACKEND` from `.env`, falls back to console
- `RateLimiter` with configurable per-event-type cooldowns

**Platform:**
- `main.py` asyncio orchestrator; SIGINT/SIGTERM → clean shutdown with file writes
- `deploy/install.sh` one-command installer; auto-detects Debian Bookworm / Trixie
- Systemd service units for Kismet, gpsd, and Passive Vigilance
- WiFi monitor mode via udev rule + NetworkManager unmanaged config (MT7610U tested)
- 139 tests passing across all modules

---

[Unreleased]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.4-alpha...HEAD
[v0.4-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.3.1-alpha...v0.4-alpha
[v0.3.1-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.3-alpha...v0.3.1-alpha
[v0.3-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.2.2-alpha...v0.3-alpha
[v0.2.2-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.2.1-alpha...v0.2.2-alpha
[v0.2.1-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.2-alpha...v0.2.1-alpha
[v0.2-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/compare/v0.1-alpha...v0.2-alpha
[v0.1-alpha]: https://github.com/Isthistak3n/Passive-Vigilance/releases/tag/v0.1-alpha
