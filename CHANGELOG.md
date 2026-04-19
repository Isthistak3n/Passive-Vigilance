# Changelog

All notable changes to Passive Vigilance are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com).

---

## [v0.4.2-alpha] — 2026-04-18

### What's better now

Field hardening and installer improvements for real-world deployments.
The sensor now survives crashes without losing buffered data, filters out
poor-quality GPS fixes before stamping detections, and the RTL-SDR scanner
throttles itself when the Pi runs hot.

- **Crash-safe emergency flush** — unhandled exceptions in the event loop
  now trigger `_emergency_flush()` before exiting, dumping all buffered
  events to `emergency_dump.jsonl` using stdlib only (no geopandas/requests)
- **Isolated shutdown steps** — each shutdown step (summary.json, shapefile,
  GeoJSON, WiGLE upload) is now wrapped in its own try/except; one failure
  no longer prevents the others from running
- **GPS fix quality filter** — `GPS_MIN_QUALITY` (`any`/`2d`/`3d`) and
  `GPS_MAX_HDOP` (default `5.0`) env vars added to `modules/gps.py`;
  low-quality fixes are rejected before stamping detection events
- **DroneRF duty cycle** — configurable rest period after each frequency
  sweep via `DRONE_RF_REST_SECONDS` (default `20`); CPU temperature
  throttle doubles rest time when `DRONE_RF_MAX_TEMP_C` (default `75°C`)
  is exceeded, preventing thermal shutdown on Pi 3B+ in enclosed cases
- **Python virtualenv installer** — all Python dependencies now install
  into `/opt/passive-vigilance/venv` with `--system-site-packages`;
  replaces `--break-system-packages`; apt-installed GIS packages
  (geopandas, fiona, GDAL) are shared without rebuilding on ARM
- **GDAL via apt** — `gdal-bin libgdal-dev python3-gdal python3-geopandas
  python3-fiona python3-numpy python3-shapely` installed via apt before
  pip; prevents 30-minute ARM source builds on fresh installs
- **GPS device from .env** — `install.sh` now reads `GPS_DEVICE` from
  `.env` before configuring gpsd; defaults to `/dev/ttyUSB0` if unset
- **`pv-python` convenience symlink** — `/usr/local/bin/pv-python` →
  `/opt/passive-vigilance/venv/bin/python3` for easy manual runs

**222 tests passing.**

---

## [v0.4-alpha] — 2026-04-18

### What's new
The sensor now has an optional live web dashboard accessible
from any browser — including remotely via Tailscale and on
the 4B+ touchscreen via Chromium.

- **Optional web GUI** — set `GUI_ENABLED=true` in `.env` to
  start a Flask web server at `http://[pi-ip]:8080`
- **Five dashboard tabs** — Dashboard, Live Map, Devices,
  Aircraft, Session
- **Live Leaflet.js map** — color-coded detection markers,
  track lines for persistent devices, aircraft at altitude
- **Server-sent events** — detections pushed to browser
  instantly as they happen, no polling
- **Touch-friendly dark theme** — optimized for 7-inch
  touchscreen, works on phone via Tailscale
- **Zero overhead when disabled** — `GUI_ENABLED=false`
  (default) leaves the orchestrator completely unchanged
- **MAC randomization handling** — detects locally-administered
  MACs, fingerprint grouping by shared probe SSIDs
- **Google Earth KML output** — color-coded by alert level,
  track lines, aircraft at actual altitude, written alongside
  shapefiles at session end
- **5-minute health banner** — periodic sensor status in logs
- **Auto-reconnect** — up to 3 attempts on sensor degradation

**207 tests passing.**

---

## [v0.3.1-alpha] — 2026-04-17

### What's better now
Patch release addressing April 17 code review findings. The
sensor platform is noticeably more robust and easier to
operate in the field.

- **Tunable poll intervals** — GPS, Kismet, ADS-B, and
  DroneRF poll rates now configurable via `.env`
- **GPS startup patience** — timeout extended to 120 seconds
  via `GPS_STARTUP_TIMEOUT_SECONDS`
- **Sensor health alerts** — clear WARNING on degradation,
  INFO on recovery
- **Atomic rate limiter writes** — file locking prevents
  corruption on rapid restarts
- **Alert retries** — exponential backoff on network failures
- **GPS device auto-detection** — scans fallback paths if
  configured device not found
- **numpy pinned** — `numpy>=1.24,<3` fixes CI conflicts
- **CI green on Python 3.11 and 3.13**

**154 tests passing.**

---

## [v0.3-alpha] — 2026-04-17

### What's new
The sensor now handles iOS and Android MAC randomization
intelligently, and produces Google Earth KML output.

- **MAC randomization detection** — locally-administered bit
  detection, no more treating every iOS probe as a new device
- **Fingerprint grouping** — clusters devices sharing probe
  SSIDs despite MAC changes between scans
- **MAC type in alerts** — every alert body now includes
  whether the device is randomized or static
- **KML output** — written automatically at session end
  alongside shapefiles
- **Three KML layers** — WiFi/BT devices, aircraft, drone RF
- **Color-coded markers** — white/yellow/orange/red by alert
  level, red for emergency aircraft
- **Track lines** — connect GPS locations where same device
  was seen

**188 tests passing.**

---

## [v0.2.2-alpha] — 2026-04-17

### What's better now
The sensor now tells you what it is doing every 5 minutes,
and recovers automatically when a sensor goes offline.

- **5-minute health banner** — GPS fix status, Kismet
  activity, ADS-B, DroneRF, alert counts, session uptime
  all logged every 300 seconds (tunable)
- **Auto-reconnect** — up to 3 attempts with 5s between
  tries when a sensor degrades mid-session
- **Version string corrected** to match Git tag

**154 tests passing.**

---

## [v0.2.1-alpha] — 2026-04-17

### What's better now
Addressed every issue from the April 17 code review.

- **Correct version string** in logs and banners
- **Tunable poll intervals** via `.env`
- **GPS timeout 120s** via `GPS_STARTUP_TIMEOUT_SECONDS`
- **Sensor health dict** with WARNING on degradation
- **Atomic rate limiter** with file locking
- **HTTP retries** with exponential backoff
- **GPS device fallback** scanning
- **numpy pinned** in requirements.txt
- **CI green** on Python 3.11 and 3.13

**145 tests passing.**

---

## [v0.2-alpha] — 2026-04-14

### What's new
First fully functional release. Every module implemented,
tested, and wired into a single asyncio orchestrator that
runs as a systemd service and starts automatically on boot.

- **Always-on sensor** — plug in power, capturing starts
  automatically via systemd
- **Counter-surveillance detection** — persistence engine
  scores WiFi and BT devices across four overlapping time
  windows (5/10/15/20 min)
- **Pluggable alerts** — Ntfy, Telegram, Discord, or Console
  via single `.env` setting with rate limiting
- **Crash-safe session logging** — incremental JSONL survives
  unexpected shutdowns
- **GIS output** — shapefile and GeoJSON at session end,
  three layers: WiFi/BT, aircraft, drone
- **WiGLE wardriving upload** — automatic at session end
- **Persistent rate limits** — cooldowns survive restarts
- **One-command installer** — `sudo bash deploy/install.sh`

**139 tests passing.**

---

## [v0.1-alpha] — 2026-04-13

### Foundation release
Sensor stack fully built and individually tested. Each module
works independently — the orchestrator comes in v0.2.

- GPS daemon with gpsd integration and fix quality filtering
- WiFi + Bluetooth via Kismet REST API, monitor mode
  auto-setup for MediaTek MT7610U and RTL8811AU dongles
- ADS-B via readsb JSON API with adsb.lol enrichment
- Drone RF scanning at 433/868/915 MHz and 2.4 GHz
- MAC/OUI/SSID ignore lists with CLI management tool
- Four-window persistence scoring with GPS clustering
- Ntfy/Telegram/Discord/Console alert backends
- One-command installer with systemd services

**112 tests passing.**

---

## About this project

Passive Vigilance is a field-deployable passive RF sensor
platform for counter-surveillance, situational awareness,
and open-source RF intelligence. It never transmits.

Inspired by [Chasing Your Tail NG](https://github.com/ArgeliusLabs/Chasing-Your-Tail-NG)
by [@matt0177](https://github.com/matt0177).
