# ADSBModule

Async client for the readsb (dump1090-fa drop-in) JSON API. Provides live aircraft positions and optional adsb.lol enrichment.

## Overview

`modules/dump1090.py` polls `aircraft.json`, normalises the records, and returns them GPS-stamped by the orchestrator. Enrichment (registration, type, operator, military flag) is optional and keyed by ICAO hex.

## Key behaviours

- **No GPS reads of its own** — the orchestrator supplies the current fix. This eliminated the gpsd-coupling failure that previously stalled both Wi-Fi and ADS-B.
- **Reconnect contract** — `poll_aircraft()` raises `ConnectionError` if called before a successful `connect()`, so the orchestrator’s reconnect path fires instead of silently stranding the module.
- **Enrichment** — `enrich_aircraft(icao)` calls the ADSBExchange / adsb.lol RapidAPI endpoint when `ADSBXLOL_API_KEY` is set; returns `{}` on any failure.
- **Hardware probe** — `is_hardware_present()` looks for known RTL-SDR USB IDs via `lsusb`.

## Workflow

```
Orchestrator obtains GPS fix
  → ADSBModule.poll_aircraft(gps_fix=…)
  → GET aircraft.json
  → normalise (icao, callsign, lat/lon, alt, speed, track, squawk, rssi, emergency)
  → stamp with observer GPS
  → orchestrator maintains per-ICAO index + thinned tracks
  → scores aircraft-of-interest (loiter / return / military / no-callsign)
  → may request an ACARS window when a contact is held > ACARS_TRIGGER_SECONDS
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `READSB_URL` | `http://localhost:8080/data/aircraft.json` | |
| `DUMP1090_HOST` | localhost | Used only if `READSB_URL` is unset |
| `ADSBXLOL_API_KEY` | (empty) | Optional enrichment |

## Pitfalls

- Returning `[]` on a failed connect permanently strands ADS-B; the raise-on-unconnected contract exists so the orchestrator can recover.
- Enrichment is best-effort and never blocks the poll path.
- When the SDR coordinator is in SHARED mode, readsb is started/stopped on the ADS-B slice; the module simply sees an empty or unreachable endpoint between slices.

## Hardware notes

The fixed node runs a single RTL-SDR time-shared with AIS/ACARS. The mobile node has no SDR, so ADS-B stays dark.

## Related modules

- [SDR Coordinator](sdr.md) — owns the dongle lifecycle in SHARED mode.
- [ACARS](acars.md) — triggered from long-held ADS-B contacts.
- [SensorOrchestrator](orchestrator.md) — scoring and track maintenance live here.
