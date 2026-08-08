# Capture Modules (Kismet, BLE, Remote ID, ADS-B, AIS, ACARS)

The capture layer turns raw RF into structured, GPS-stamped device records that the orchestrator can score, store and display. All modules follow the same lifecycle (`connect` / `poll` or `drain` / `close`) and degrade gracefully when hardware is absent.

## Overview

| Module | Source | What it produces | Default |
|---|---|---|---|
| **KismetModule** | Kismet REST API | WiFi + BT device records (probes, beacons, signal) | Always on |
| **BLEScanner** | Raw HCI (hci0) | Passive BLE advertisements with real RSSI | Opt-in (`BLE_SCANNER_ENABLED`) |
| **RemoteIDModule** | Kismet 802.11 vendor IE | FAA Remote ID (ASTM F3411) drone frames | Always attempted |
| **ADSBModule** | readsb JSON | Aircraft positions + enrichment | Always on when SDR present |
| **AISModule** | AIS-catcher UDP JSON | Marine vessels | Opt-in (`AIS_ENABLED`) |
| **ACARSModule** | acarsdec / dumpvdl2 UDP JSON | Aviation datalink messages | Opt-in (`ACARS_ENABLED`) |

## KismetModule (`modules/kismet.py`)

Async REST client that polls `/devices/views/all/devices.json` with a minimal field set.

### Key behaviours

- **Cookie auth** (`KISMET=<token>`) — required by Kismet 2025.09+.
- **Active-window filter** (`KISMET_ACTIVE_WINDOW_SECONDS`, default 300). Kismet’s device list is permanent for the session; without the filter a departed device continues to be stamped with new GPS positions (mobile) or hour-mask bits (fixed). Set to 0 only to deliberately restore the full list.
- **Ignore-list integration** — MACs / SSIDs / OUIs are filtered before the list is returned.
- **Probe extraction** — named SSIDs only; the empty/wildcard probe is dropped.
- **AP beacon context** — `beaconed_ssid`, channel and crypt for access points.
- **Offline OUI fallback** — when Kismet’s manufacturer field is empty or “Unknown”.
- GPS stamping is performed by the orchestrator; the module itself never opens gpsd.

### Pitfalls

- Kismet leaf-key gotcha: slash-path fields are returned under the *leaf* key.
- Zero RSSI is a placeholder, not a measurement.
- Monitor-mode check shells out to `iw`; the call is offloaded to a worker thread so it never blocks the event loop.

## BLEScanner (`modules/ble_scanner.py`)

Raw-HCI passive advertisement capture that replaces Kismet’s empty `linuxbluetooth` feed.

### Design points

- **Passive only** (`scan_type=0x00`) — the radio never transmits.
- Exclusive use of the controller; Kismet’s Bluetooth source and `bluetoothd` must be removed / stopped.
- Requires `CAP_NET_RAW` + `CAP_NET_ADMIN` (granted by the service unit).
- Auto-detects the HCI index (`BLE_HCI_DEVICE` override or lowest present controller) so a USB dongle that re-enumerates after a reset is still found.
- Parses manufacturer data, service UUIDs (16/32/128-bit), solicited UUIDs, local name, appearance, TX power and directed-advert flags.
- Real RSSI is recovered; randomized addresses still dominate but a static vendor address resolves via the offline OUI database.
- On controller loss (USB drop) the scanner tears itself down and flips `available=False` so the orchestrator’s radio-health check can alert.

The orchestrator buffers the latest advert per address and merges the buffer into the Kismet device list on every poll, so BLE flows through the same entity / scoring / GUI path as WiFi.

## RemoteIDModule (`modules/remote_id.py`)

Parses FAA Remote ID (ASTM F3411-22a) vendor-specific information elements (OUI `FA:0B:BC`) that appear in 802.11 beacons captured by Kismet. Extracts UAS ID, operator / drone positions, UA type and status. One event per UAS ID is kept in memory with a thinned flight-path track; alerts are rate-limited per UAS ID.

## ADSBModule (`modules/dump1090.py`)

Polls readsb’s JSON endpoint (`/data/aircraft.json`). The orchestrator stamps GPS, maintains a per-ICAO index with thinned tracks, scores “aircraft of interest” (loiter / return / military / no-callsign), and can trigger a bounded ACARS decode window when a contact is held continuously past `ACARS_TRIGGER_SECONDS`.

## AISModule & ACARSModule

Both consume JSON over localhost UDP from their respective decoder services.

- **AIS** — vessels carry their own position; a range gate drops implausibly distant reports when the node has a GPS fix.
- **ACARS** — messages are correlated back to live ADS-B contacts by tail ↔ registration, flight-id ↔ callsign, or (fallback) nearest position within `ACARS_POSITION_MATCH_KM`. On a single dongle the decode window is requested via `SDRCoordinator.request_band_window`. See the dedicated ACARS discussion in the module source and the orchestrator docs for correlation details.

## Configuration highlights

| Variable | Module | Default |
|---|---|---|
| `KISMET_API_KEY` | Kismet | (required) |
| `KISMET_ACTIVE_WINDOW_SECONDS` | Kismet | 300 |
| `BLE_SCANNER_ENABLED` | BLE | false |
| `BLE_HCI_DEVICE` | BLE | auto |
| `AIS_ENABLED` / `ACARS_ENABLED` | AIS / ACARS | false |
| `ACARS_TRIGGER_SECONDS` | ACARS | 30 |
| `READSB_URL` | ADS-B | `http://localhost:8080/data/aircraft.json` |

## Hardware notes

| Node | Capture stack |
|---|---|
| **chase** | wlan1 (RTL8811CU monitor) + USB BT dongle (hci0) + RTL-SDR (ADS-B/AIS/ACARS time-share) + Remote ID via Kismet |
| **survkis** | wlan1 (RTL8811CU) + u-blox GPS; no SDR, so ADS-B/AIS/ACARS stay dark |

## Related modules

- [SensorOrchestrator](orchestrator.md) — sole consumer of the poll / drain results.
- [SDR Coordinator](sdr.md) — time-shares the single RTL-SDR among ADS-B, AIS and ACARS.
- [Identity layer](identity.md) — fingerprints the records produced here.
- `modules/ignore_list.py` — filters before the list reaches scoring.

## See also

- [docs/setup.md](../setup.md) — per-sensor bring-up and troubleshooting.
- [docs/wifi-driver-8812au.md](../wifi-driver-8812au.md) — optional driver swap.
