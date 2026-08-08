# KismetModule

Primary Wi-Fi (and residual Bluetooth) capture path. Talks to the Kismet sensor daemon over its REST API and returns GPS-stamped device records for the orchestrator.

## Overview

`modules/kismet.py` is an async REST client. It is always constructed; without a reachable Kismet the node has no Wi-Fi picture.

| Responsibility | Detail |
|---|---|
| Auth | Cookie `KISMET=<token>` (required by Kismet 2025.09+) |
| Endpoint | `POST /devices/views/all/devices.json` with a minimal field filter |
| Filtering | Ignore-list + active-window recency filter |
| Output | List of device dicts ready for identity, scoring, and EntityStore |

## Design rationale

Kismet’s device list is **permanent for the session**. An unfiltered poll therefore re-processes every device ever heard, which:

- On mobile nodes creates false “followers” (a device passed ten minutes ago still accumulates GPS clusters from the node’s own movement).
- On fixed nodes smears a passer-by into every subsequent hour-mask bit and keeps re-flagging departed devices after freeze.

The active-window filter (`KISMET_ACTIVE_WINDOW_SECONDS`, default 300) drops anything whose Kismet `last_time` is older than the window before the list ever reaches scoring or baseline learning.

GPS stamping is performed by the orchestrator. The module never opens gpsd itself — that coupling previously let a silent gpsd wedge both the Wi-Fi and ADS-B pollers.

## Key behaviours

- **Probe extraction** — named SSIDs only; the empty/wildcard probe is dropped.
- **AP beacon context** — `beaconed_ssid`, channel, and crypt for access points (used by EntityStore network-affinity and beacon evidence).
- **Offline OUI fallback** — when Kismet’s manufacturer field is empty or “Unknown”.
- **Monitor-mode check** — shells out to `iw` on a worker thread so it never blocks the event loop.
- **Ignore-list** — MACs, SSIDs, and OUIs are filtered before the list is returned.

## Workflow

```
Orchestrator obtains GPS fix (or None)
  → KismetModule.poll_devices(gps_fix=…)
  → POST devices.json with field filter
  → active-window + ignore-list filter
  → enrich with mac_type, is_randomized, probe_ssids, beacon fields
  → return list of records
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `KISMET_HOST` / `KISMET_PORT` | localhost:2501 | |
| `KISMET_API_KEY` | (required) | Generated in Kismet web UI |
| `WIFI_MONITOR_INTERFACE` | wlan1 | Used only for the monitor-mode warning |
| `KISMET_ACTIVE_WINDOW_SECONDS` | 300 | 0 = keep full cumulative list |

## Pitfalls

- Kismet leaf-key gotcha: slash-path fields are returned under the *leaf* key (`kismet.common.signal.last_signal`, not the full path).
- Zero RSSI is a placeholder, not a measurement; downstream code treats it like a missing reading.
- Leaving the active window at 0 on a fixed node is the classic baseline-smear failure mode.
- API key must be present as a cookie, not the older header form.

## Hardware notes

Both the fixed and mobile nodes run an RTL8811CU (or equivalent) in monitor mode as `wlan1`. Kismet is the only source of Wi-Fi probes and beacons.

## Related modules

- [SensorOrchestrator](orchestrator.md) — sole consumer.
- [Identity](identity.md) — fingerprints the records produced here.
- [Durable Stores](stores.md) — EntityStore records every poll.
- `modules/ignore_list.py` — filters before the list reaches scoring.
