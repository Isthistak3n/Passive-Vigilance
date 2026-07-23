# Architecture & source map

This is the developer's map of the codebase — where each responsibility lives.
For a plain-language description of *what* the system does and how the sensors
feed each other, see the "How it works" diagram in the [README](../README.md).
For the design rationale behind these modules, see
[design-and-roadmap.md](design-and-roadmap.md).

## Runtime shape

Passive Vigilance runs as a set of background systemd services. Four capture
daemons (`gpsd`, `kismet`, `readsb`, and — when enabled — the VHF decoders) feed
a single Python orchestrator built on `asyncio`. The orchestrator polls each
source on its own interval, stamps every detection with a GPS fix, runs it
through the scoring engine chosen by `NODE_MODE`, and writes the results to
SQLite, to per-session GIS files, and to the optional web dashboard.

## Source tree

```
Passive-Vigilance/
├── main.py                           # asyncio orchestrator; loads .env; SIGINT/SIGTERM shutdown
├── requirements.txt                  # Python dependencies
├── .env.example                      # Environment variable template (never commit .env)
├── core/
│   ├── exceptions.py                 # Custom exception hierarchy + ErrorSeverity enum
│   └── logging.py                    # Structured logger factory — consistent log format across modules
├── modules/
│   ├── gps.py                        # GPSModule — gpsd streaming client; position/time backbone
│   ├── kismet.py                     # KismetModule — Kismet REST API; async WiFi + BT polling; probe-SSID + fingerprint extraction
│   ├── dump1090.py                   # ADSBModule — readsb JSON; aircraft polling + adsb.lol enrichment
│   ├── ais.py                        # AISModule — AIS-catcher JSON over UDP; marine vessel tracking (optional/VHF)
│   ├── acars.py                      # ACARSModule — acarsdec/dumpvdl2 JSON; aviation datalink decode (optional/VHF)
│   ├── aircraft_registry.py          # AircraftRegistry — connectivity-adaptive ICAO→registration (offline DB + adsb.lol)
│   ├── drone_rf.py                   # DroneRFModule — RETIRED (default off; replaced by the SDR decode cycle; kept for reversibility)
│   ├── remote_id.py                  # RemoteIDModule — FAA Remote ID (ASTM F3411-22a) via Kismet 802.11 vendor IE
│   ├── ble_scanner.py                # BLEScanner — passive raw-HCI LE advertisement capture (vendor/services/name + real RSSI)
│   ├── ble_fingerprint.py            # BLE advertisement → rotation-resistant signature (ble-fp:)
│   ├── wifi_fingerprint.py           # WiFi probe SSIDs + IE-set hash → rotation-resistant signature (wifi-fp:)
│   ├── device_identity.py            # Shared strong/medium contact-identity tiers used by both scoring engines
│   ├── copresence.py                 # Cross-PHY co-presence linking — a person's Wi-Fi + BLE radios into one "person" (over-merge-safe)
│   ├── contact_designator.py         # CLASS-IDENT-# naval/air-style track labels for WiFi/BT contacts
│   ├── sdr_manager.py                # SDRManager — RTL-SDR inventory detection; SDRMode resolution
│   ├── sdr_coordinator.py            # SDRCoordinator — asyncio time-share scheduler for single-dongle setups
│   ├── sdr_utils.py                  # Shared RTL-SDR hardware detection utilities
│   ├── orchestrator.py               # SensorOrchestrator — module lifecycle and health management
│   ├── ignore_list.py                # IgnoreList — MAC/OUI/SSID filter; atomic JSON persistence
│   ├── mac_utils.py                  # MAC randomization detection, type classification, fingerprinting
│   ├── alerts.py                     # AlertBackend ABC + Ntfy / Telegram / Discord / Console backends
│   ├── kml_writer.py                 # KMLWriter — Google Earth KML with color-coded placemarks and track lines
│   ├── persistence.py                # PersistenceEngine — mobile (location-diversity) scoring; DetectionEvent dataclass
│   ├── scoring_engine.py             # ScoringEngine ABC — strategy interface (update + status) selected by NODE_MODE
│   ├── fixed_scoring.py              # FixedScoring — fixed-node baseline-deviation (novelty + off-schedule) scoring
│   ├── baseline_store.py             # BaselineStore — durable SQLite baseline; crash-safe learning window; hour-mask + RSSI stats
│   ├── entity_store.py               # EntityStore — durable SQLite (probe evidence, fingerprint, entities, observation history)
│   ├── survey_store.py               # SurveyStore — durable SQLite for recon-pair taskings, mobile observations, wardrive index, bed-down findings
│   ├── survey_sync.py                # SurveySync — mobile-node client to the fixed node's survey endpoints (store-and-forward)
│   ├── survey_coordinator.py         # SurveyCoordinator — recon-pair logic: mobile matcher, fixed-node tasking, patrol-aware sync
│   ├── probe_analyzer.py             # ProbeAnalyzer — WiFi probe pattern analysis
│   ├── shapefile.py                  # ShapefileWriter — geopandas/fiona; detections as .shp point features
│   ├── wigle.py                      # WiGLEUploader — upload Kismet CSV to WiGLE.net at session end
├── gui/
│   ├── server.py                     # GUIServer — Flask in daemon thread; SSE /stream; REST /api/*; mode toggle (/api/mode)
│   ├── templates/
│   │   ├── index.html                # Fixed-node SPA; tabs (incl. Remote ID, Survey); Leaflet map; SSE client
│   │   └── mobile.html               # Map-less mobile-node SPA; Nearby + Survey/patrol tabs (served when NODE_MODE=mobile)
│   └── static/
│       ├── app.js                    # Fixed-node SSE client; Leaflet markers; table rendering; tab switching
│       ├── mobile.js                 # Mobile-node SSE client; proximity feed; patrol controls
│       └── style.css                 # Dark theme; KML-matched alert colors; touch-friendly
├── tests/                            # One test file per module (tests/test_<module>.py)
├── scripts/
│   └── manage_ignore_list.py         # CLI: add/remove MAC, OUI, SSID; --import-kismet bulk add
├── deploy/
│   ├── install.sh                    # One-command installer; auto-detects Debian/Raspberry Pi OS
│   ├── kismet.service                # Kismet systemd unit
│   ├── passive-vigilance.service     # Orchestrator systemd unit
│   ├── gpsd.override.conf            # gpsd drop-in config to add -n flag
│   ├── 99-wlan1-monitor.rules        # udev rule — set wlan1 to monitor mode at boot/plug-in
│   └── 99-unmanaged-wlan1.conf       # NetworkManager: mark wlan1 as unmanaged
├── docs/                             # This folder — see docs/README.md for the index
└── data/
    └── ignore_lists/                 # MAC/OUI/SSID ignore list JSON files (git-ignored)
```

## Testing

Every module has a matching `tests/test_<module>.py`. The suite runs in CI on
every pull request; run it locally with `pytest`. Contributors should add or
update the matching test file with any change to a module — see
[CONTRIBUTING.md](../CONTRIBUTING.md).
