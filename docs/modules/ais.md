# AISModule

Passive marine vessel tracking via an AIS-catcher JSON feed. Optional / best-effort.

## Overview

AIS is marine VHF (~161.975 / 162.025 MHz). It will not receive on a 1090 MHz ADS-B antenna and degrades rapidly inland, so it is **off by default** (`AIS_ENABLED`).

The AIS-catcher decoder runs as a systemd service — started/stopped by the SDR coordinator on the AIS slice (single dongle) or continuously on a dedicated VHF dongle. This module only listens on a localhost UDP socket and buffers parsed vessel reports.

## Key behaviours

- **Vessel position comes from the AIS message itself** — the module performs no GPS reads.
- Accepts both position reports (lat/lon) and static reports (name / ship type); the orchestrator deduplicates by MMSI and merges the two.
- AIS “not available” sentinels (lat 91.0 / lon 181.0) are mapped to `None`.
- `can_scan` / `auto_disabled` flags mirror the other SDR bands so the coordinator and GUI treat AIS uniformly.
- A range gate in the orchestrator drops implausibly distant reports when the node has a GPS fix.

## Workflow

```
AIS-catcher (systemd) → UDP JSON on 127.0.0.1:10110
  → AISModule._ingest → _parse → buffer
  → orchestrator drain_detections()
  → dedup by MMSI, optional range gate, GUI / GIS
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `AIS_ENABLED` | false | Opt-in |
| `AIS_UDP_HOST` / `AIS_UDP_PORT` | 127.0.0.1:10110 | |

## Pitfalls

- Without a VHF antenna the feed is empty; the module stays healthy and simply produces nothing.
- The decoder service must be under the same sudoers-scoped systemctl control as readsb so the coordinator can start/stop it cleanly.

## Hardware notes

On the fixed node the single RTL-SDR is time-shared (`adsb:…,ais:…`). A VHF antenna is required for useful range. The mobile node has no SDR.

## Related modules

- [SDR Coordinator](sdr.md)
- [ADSBModule](adsb.md) — sibling SDR band
- [SensorOrchestrator](orchestrator.md)
