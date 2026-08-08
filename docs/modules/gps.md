# GPSModule

Position and time backbone for the entire system. Every device record that reaches scoring, EntityStore, or GIS is stamped with the current fix (or explicitly left null).

## Overview

`GPSModule` (`modules/gps.py`) owns a dedicated reader thread that talks to gpsd. The asyncio poll loops never open the gpsd socket themselves; they only ever call `get_fix()` which returns the latest validated fix (or `None`).

This separation was introduced after a silent gpsd wedge coupled the Wi-Fi and ADS-B pollers and stalled both past the watchdog.

## Design rationale

- **Never block the event loop.** gpsd I/O and quality filtering live in a background thread.
- **Quality gates.** A fix is only published when it passes configurable HDOP / satellite / mode thresholds.
- **Fail open for position.** When no fix is available, downstream code receives `None` and continues; scoring and stores tolerate missing coordinates.

## Key behaviours

- Reader thread polls gpsd, applies quality filters (`GPS_MIN_QUALITY`, `GPS_MAX_HDOP`, etc.), and publishes the latest good fix under a lock.
- `get_fix()` is non-blocking and returns a dict with `lat`, `lon`, `utc`, and quality metadata, or `None`.
- The module can be constructed without a live gpsd (tests / headless) and simply never produces fixes.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GPS_MIN_QUALITY` | (see source) | Mode / satellite floor |
| `GPS_MAX_HDOP` | (see source) | Horizontal dilution ceiling |
| gpsd host/port | localhost defaults | |

## Pitfalls

- A wedged gpsd that never produces a TPV sentence leaves the whole system without positions; the orchestrator continues but GIS and mobile scoring degrade.
- Quality thresholds that are too strict on a marginal antenna produce long stretches of `None` fixes.
- Never call gpsd from the asyncio thread — that is the exact coupling this module was written to eliminate.

## Hardware notes

| Node | GPS |
|---|---|
| **Fixed node** | u-blox (or equivalent) via gpsd |
| **Mobile node** | u-blox; critical for mobile location-diversity scoring |

## Related modules

- [SensorOrchestrator](orchestrator.md) — sole consumer of `get_fix()`.
- [Capture](capture.md) — receives the fix as `gps_fix=` on every poll.
- [Scoring](scoring.md) — mobile engine uses successive fixes for location diversity.
