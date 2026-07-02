# Changelog

All notable changes to Passive Vigilance are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com).

---

## [Unreleased]

### What's better now

- **A randomized device that leaves and comes back is recognised as the same
  contact.** Modern phones rotate their address constantly, so a returning device
  used to reappear as a brand-new contact. Now a device is identified by *what it
  broadcasts* at one of two confidence tiers, and its rotating addresses collapse
  into a single contact line on the dashboard (with a count of how many addresses
  it has cycled through). A device that leaves the area and returns is flagged as
  a **return** rather than shown as new.
- **The platform remembers devices across days and restarts.** A contact seen on a
  previous session or a previous day is now flagged as a **returning entity**
  ("seen before") — the "has this been here before / are you being cased?" signal.
  This memory is durable, so it survives reboots.
- **A person's devices are grouped together.** When a phone appears as *both* a
  Wi-Fi client and a Bluetooth device — or someone carries a phone and a wearable —
  the radios that travel together are linked into one **person**, so one person
  isn't counted as several separate threats. Linking is deliberately conservative:
  a device only joins a person on strong, sustained co-presence, and access points
  are never grouped as people.
- **Residents vs. visitors.** The access points around a fixed node now serve as the
  "environment." A mobile device that probes for a network that exists here, or that
  the node already learned during its baseline, is a **resident**; a brand-new device
  with no tie to any local network that lingers is flagged as a **visitor of
  interest**. This is a display/awareness signal — it never changes what pages you.
- **ACARS actually enriches the aircraft view.** Decoded datalink messages now yield
  an aircraft's origin and destination and, where present, position reports; those
  bind a message to the nearest aircraft even when it broadcasts no callsign, and the
  enrichment is shown on the aircraft row and survives a page refresh.
- **The SDR pivot — DroneRF retired, maritime + aviation decode added.** The single
  dongle now runs an N-band time-share cycle: ADS-B plus optional AIS (marine vessels)
  and ACARS (aviation datalink, triggered when a contact is held in view past 30 s and
  correlated back to the aircraft by tail/flight-id). DroneRF is retired — kept in the
  tree for reversibility but off by default. AIS and ACARS are antenna-gated (they need
  a VHF antenna) and ship default-off; both are in on-Pi stress-testing.
- **Aircraft reliably appear on the live map.** A plane visible to readsb but whose
  position wasn't advancing (fringe reception, a slow or hovering target) could sit in
  the table yet never get a map marker. Positioned contacts are now pushed to the live
  map every poll, so the marker shows and tracks even when the fix is momentarily frozen.
- **GPS fixes no longer lag.** The receiver feed is now consumed on a dedicated reader
  thread, so the position the platform stamps onto every detection stays current instead
  of drifting behind real time. The old single-read-per-poll path fell progressively
  further behind a busy gpsd — minutes to hours on long runs. Harmless on a fixed node
  (its position is constant) but a real correctness fix for mobile nodes, where every
  detection was being stamped with a stale location.

### Changed

- **Dashboard map is back to plain online OpenStreetMap.** The offline-basemap feature
  (a bundled MBTiles pack served by the node) made the field map unreliable, so it was
  reverted in full — the map, the pack reader/writer, the boot-time provisioning, the
  build/export scripts, and the related settings are all removed. The browser draws OSM
  tiles directly again (the operator's view area is visible to OSM — an accepted trade).

---

## [v0.7.0-alpha] — 2026-06

### What's better now

The fixed node now identifies devices by *what they broadcast* (not their rotating
address), keeps a durable operator view, and survived two multi-day validation soaks.

- **Devices tracked across MAC/address rotation** — a randomizing phone is now keyed
  by its probe/advertisement fingerprint, so it stays one contact as its address
  changes. This cut the post-freeze "everything looks brand-new" flood from ~36 to a
  handful per cycle. Passive Bluetooth capture runs over a raw HCI socket (listen
  only) and recovers a real signal strength Kismet's BT feed never gave.
- **Contacts get stable track labels** — WiFi/BT devices show as `CLASS-IDENT-#`
  (e.g. `PHONE-LINKSYS-3`), with the number persisted against the fingerprint so it
  survives rotation and restart.
- **The dashboard remembers** — WiFi/BT, Aircraft, Drone, Remote ID, and Alerts all
  rebuild from the on-disk history across a page refresh **and** a service restart,
  including each contact's score. Alerts are persisted and shown for the first time.
- **Quieter, sharper alerts** — two false-positive floods (post-freeze novelty, then
  off-schedule on held randomized addresses) are fixed; only confident detections page
  the operator, lower-confidence ones stay on-screen but silent. Soak #3 ran ~42 h
  post-freeze with a livable alert rate, zero dropped, and flat memory.
- **The air picture works** — aircraft show as decaying current-sky markers plus a
  retained table, tracks are bounded, a returning airframe is recognised as the same
  contact, and a Remote ID tab surfaces detected UAS. Aircraft are now *scored* —
  orbit/loiter near the node is flagged while transit traffic is not.
- **ADS-B and Drone RF share one SDR** — a coordinator time-shares the single dongle
  cleanly (no more receiver wedging), and a reconnect fix means ADS-B recovers on its
  own instead of silently going dark after a restart.
- **Known networks + reconnect signals** — the WiFi/BT tab now shows each contact's
  accumulated preferred-network list ("former networks") and whether a BT device is
  calling out to reconnect, both in the CSV export.
- **Sort, filter, export** — every table tab has sortable columns, dropdown + min-count
  filters, a live count, CSV export, and sticky per-tab view settings.

### Under the hood

- New modules: `ble_scanner`, `ble_fingerprint`, `wifi_fingerprint`, `device_identity`,
  `contact_designator`, `air_geometry`, `air_scoring`, `sdr_coordinator`.
- EntityStore gained per-IE-hash PNL accumulation (`pnl_evidence`); the BLE advert
  parser keeps directed-advert / solicited-service / 128-bit-UUID / manufacturer-data
  structure. Fingerprint enrichment is capture/display only — scoring key unchanged.
- Deploy hardening: SDR handoff settle barrier, BLE controller raised LE-on at boot,
  refreshed `setup.sh`/service units, `READSB_URL` for the ADS-B JSON endpoint.
- Validation: soaks #2 and #3 on the node (see `docs/field-findings-2026-06.md`).
- Test suite grew to 687 passing.

---

## [v0.6.0-alpha] — 2026-06

### What's better now

Hardened for days-long unattended running, with the fixed-node detector and the
dashboard both noticeably sharper.

- **Runs for days without leaking** — every per-poll detection stream (WiFi,
  aircraft, drone, Remote ID) now collapses repeated sightings of one entity into
  a single ongoing detection instead of growing a row per poll, and the
  observation history is bounded by a time-based retention sweep. Validated on the
  node across a forced baseline freeze — the gap that left post-freeze running
  unproven (#74).
- **Aircraft show as tracks** — a plane is now one detection carrying its flight
  path (drawn as a line in the Google Earth export) instead of a scatter of
  points; the dashboard's aircraft list de-duplicates a moving plane and now also
  lists aircraft that report without a position (#74, #80).
- **"Something is closing in"** — a fixed node flags a known device whose signal is
  trending meaningfully stronger than its learned baseline (physically
  approaching), with noise guards and access points excluded (#75).
- **A stalled sensor no longer looks healthy** — a watchdog flips a sensor to
  degraded and alerts when its polling goes silent, catching the case where the
  node sat green while quietly capturing nothing (#79).
- **The dashboard survives a restart** — it retries binding its port instead of
  dying silently when a fast restart still holds the old one, and logs a clear
  error if it truly cannot start (#79).
- **Baseline state at a glance** — a header strip shows whether the node is still
  learning (with a countdown to freeze) or frozen and watching for deviations (#80).
- **Bluetooth survives reboots** — the USB Bluetooth controller is raised before
  Kismet reads its sources, so BT capture returns on its own after a restart
  instead of needing a manual re-enable (#76).

### Under the hood

- New: a sensor stall watchdog and observation-history retention; per-ICAO
  aircraft tracks and per-band drone / per-UAS Remote ID de-duplication.
- The agent and contributor docs were de-duplicated so each topic has one home
  (#81).
- Test suite grew to 417 passing.

---

## [v0.5.0-alpha] — 2026-06

### What's better now

The node now understands whether it is moving or stationary and scores threats
accordingly, and it has begun building a durable memory of the devices around it.

- **Fixed vs. mobile detection modes** — a required `NODE_MODE` (`fixed` or
  `mobile`, no silent default) forks the scoring strategy. Mobile keeps the
  existing location-diversity model; fixed learns the location's normal RF
  "pattern of life" over a configurable window and then flags deviations. This
  resolves the long-standing issue where a stationary node never alerted
  (#50, #66).
- **Fixed-mode pattern-of-life** — flags novel devices that appear and linger,
  and known baseline devices seen off their usual hour-of-day, with graduated
  severity (suspicious → likely → high). Off-schedule only activates once a
  device's baseline is rich enough to define a schedule, so thin baselines don't
  false-alarm (#68, #69).
- **Durable, crash-safe baseline** — the learned baseline lives in SQLite and
  survives restarts and reboots; a crash loop resumes the existing learning
  window instead of starting over.
- **In-dashboard mode toggle** — switch a node between fixed and mobile from the
  web GUI (requires `GUI_TOKEN`); the control makes the restart requirement
  explicit (#67).
- **Probe-SSID and fingerprint capture** — each device record now carries the
  networks it is probing for and Kismet's own probe fingerprint, the basis for
  recognising a device across sessions despite MAC randomization (#70).
- **Entity/observation store** — every poll is recorded into durable SQLite
  (probe evidence, per-device fingerprint, one entity per device, and a growing
  observation history), recorded at the capture layer for both modes (#71).
- **Bluetooth capture enabled** — a USB Bluetooth dongle is supported as a Kismet
  source for BT/BLE devices, sidestepping the onboard-Bluetooth/GPS-HAT UART
  conflict (#48).

### Under the hood

- New modules: `scoring_engine.py` (strategy interface), `fixed_scoring.py`,
  `baseline_store.py`, `entity_store.py`.
- Test suite grew to 368 passing.

---

## [v0.4.3-alpha] — 2026-05-17

### What's better now

RTL-SDR dongle now works on a fresh install and the SDR coordinator
reliably hands the dongle between ADS-B and drone scanning. Six fixes
from the Pi 4 smoke test, plus three smaller cleanups.

- **wiedehopf readsb** — installer builds RTL-SDR-capable readsb from
  source; the Debian Trixie package lacks `ENABLE_RTLSDR` and crash-looped
  with 181 restarts on prod
- **tar1090 + port 8080** — tar1090 web interface installed; lighttpd
  patched to serve `/data/aircraft.json` on port 8080 (matches PV default)
- **READSB_URL configurable** — ADS-B JSON endpoint now read from
  `READSB_URL` env var; default `http://localhost:8080/data/aircraft.json`
- **DVB blacklist survives reboot** — blacklist file renamed to `.conf`
  (initramfs-tools ignores `.rules`); `install /bin/false` directives added;
  `update-initramfs -u` called; legacy `rtlsdr.rules` removed
- **GPS detects ttyACM\*** — installer now probes `/dev/ttyUSB*` and
  `/dev/ttyACM*` (CDC-ACM u-blox dongles)
- **SDR coordinator sudo fix** — `systemctl start/stop readsb` now prefixed
  with `sudo`; scoped sudoers rule written by installer; eliminates
  "Interactive authentication required" that blocked every SDR handoff
- **Health banner shows all 6 modules** — DroneRF was unconditionally listed
  even when absent (false-green); RemoteID was never listed at all
- **`datetime.utcnow()` replaced** — deprecated in Python 3.12+; replaced
  with `datetime.now(timezone.utc)` in `modules/alerts.py`
- **Test class typo fixed** — `TestDroneRFDDrainDetections` → `TestDroneRFDrainDetections`

**280 tests passing.**

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
