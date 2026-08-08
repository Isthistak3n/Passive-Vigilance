# ACARSModule

Passive aviation datalink decode via an acarsdec / dumpvdl2 JSON feed. Optional / best-effort.

## Overview

ACARS is aviation VHF (legacy ~131 MHz via acarsdec; modern VDL Mode 2 ~136 MHz via dumpvdl2). It will not receive on a 1090 MHz ADS-B antenna, so it is **off by default** (`ACARS_ENABLED`).

**ACARS is plaintext — this module decodes it, it does not decrypt.** It is also a shared broadcast channel: you receive every aircraft in range, not a chosen target. The orchestrator correlates each decoded message back to a live ADS-B contact.

The decoder runs as a systemd service. On a single dongle it is started on demand via `SDRCoordinator.request_band_window("acars", …)` when an ADS-B contact has been held continuously past `ACARS_TRIGGER_SECONDS`. On a dedicated VHF dongle it can run continuously.

## High-level flow

1. External decoder emits line-delimited JSON to `127.0.0.1:5555`.
2. `ACARSModule` listens, normalises, classifies, and buffers.
3. Orchestrator drains the buffer and correlates to live ADS-B contacts.
4. Matched messages are attached to the aircraft event and surfaced in the GUI / `acars.jsonl`.

## What the module does

### Ingestion
- `asyncio` datagram endpoint.
- Each JSON line is passed to `_parse()`.

### Normalisation
Handles both decoder formats:
- Flat-ish `acarsdec` objects.
- Nested `dumpvdl2` objects (`vdl2 → avlc → acars`).

Extracts: tail / registration, flight ID, ARINC label, free-text body, origin / destination, position (structured fields first, then three strict regexes for decimal, degree-minute, and the common AOC “POSN21207W157466” form).

### Classification & enrichment
- Tries **application-layer** decode first (CPDLC, ADS-C, MIAM, media advisory) by walking the nested tree that libacars already produced.
- Falls back to text classification: Position report, Performance / engine, OOOI, Route / dispatch, Link management, Free text / other.
- Surfaces only fields that can be named with high confidence. Proprietary deep columns stay in the raw text — the code never invents structure.

### Output record
```python
{
  "tail", "flight_id", "label", "label_name",
  "category", "fields",          # human-readable breakout
  "text", "origin", "destination",
  "lat", "lon",
  "cver", "timestamp"
}
```

`drain_detections()` returns and clears the buffer (same contract used by AIS).

## How the orchestrator uses it

- On every ADS-B poll, if a contact has been held past `ACARS_TRIGGER_SECONDS` (default 30), it requests a short ACARS window from the SDR coordinator.
- Registration is resolved so tail matching works.
- Incoming messages are correlated by:
  1. Tail ↔ registration
  2. Flight-id ↔ callsign
  3. Nearest position within `ACARS_POSITION_MATCH_KM` (default 50 km)
- Matched messages are attached under `event["acars"]`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ACARS_ENABLED` | false | Opt-in |
| `ACARS_UDP_HOST` / `ACARS_UDP_PORT` | 127.0.0.1:5555 | |
| `ACARS_TRIGGER_SECONDS` | 30 | Hold time before requesting a window |
| `ACARS_POSITION_MATCH_KM` | 50 | Fallback correlation radius |
| `ACARS_MAX_WINDOWS_PER_CYCLE` | 4 | Cap so ACARS cannot starve ADS-B |

## Pitfalls

- Without a VHF antenna the feed is empty; the module stays healthy and simply produces nothing.
- Correlation is best-effort; many messages remain unmatched and still appear in the raw ACARS feed.
- `reclassify()` lets the GUI re-apply newer parsers to old records without rewriting the log (schema version `cver`).

## Hardware notes

On the fixed node the single RTL-SDR is time-shared; ACARS is requested only on long-held contacts. A VHF antenna (often via SMA splitter) is required. The mobile node has no SDR.

## Related modules

- [ADSBModule](adsb.md) — source of the trigger and correlation target.
- [SDR Coordinator](sdr.md) — owns the on-demand window.
- [SensorOrchestrator](orchestrator.md) — correlation and event attachment.
