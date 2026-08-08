# SDR Coordination

When only one RTL-SDR dongle is present, **SDRCoordinator** time-shares it across an ordered cycle of bands (ADS-B, AIS, ACARS, optional DroneRF). In DEDICATED mode (≥ 2 dongles) the coordinator is bypassed and each band runs continuously.

## Overview

| Mode | Behaviour |
|---|---|
| `SHARED` (1 dongle) | Ordered cycle + optional preemption windows |
| `DEDICATED` (≥ 2 dongles) | Each band owns its own dongle; coordinator not started |
| `AUTO` | Resolved from `detect_sdr_count()` |

## Design rationale

A single RTL-SDR can only be tuned to one frequency at a time. The coordinator therefore:

- Holds an exclusive asyncio lock around every handoff.
- Confirms the outgoing owner has *released* the dongle before the next owner acquires it (honest-release).
- Inserts a settle barrier so libusb / kernel fully free the device.
- Supports a one-shot preemption window (`request_band_window`) used by the >30 s ACARS trigger.
- Reclaims orphaned decoder services left running by a previous crash.

These measures eliminated the multi-hour “SDR wedged / Device or resource busy” crash loops observed in 2026 field soaks.

## Cycle configuration

```
SDR_CYCLE_SLICES=adsb:600,ais:30
```

ACARS is deliberately *not* a regular cycle slice; it is requested only when an aircraft is held continuously past `ACARS_TRIGGER_SECONDS`.

## Key methods

- `start()` / `stop()` — reclaim orphans, hand off to ADS-B, restore readsb on exit.
- `_coordinator_loop()` — walk the slices, honour pending windows.
- `request_band_window(band, duration)` — queue a preemption.
- `_handoff_to(band)` — lock, release, settle, acquire, update health.

Decoder services are started/stopped via a scoped sudoers rule (`systemctl start/stop <service>`).

## Pitfalls

- A decoder that overruns its slice and refuses to stop parks the coordinator on that band rather than starting readsb onto a busy device.
- Zero / negative slice lengths are dropped to avoid tight handoff churn.
- `healthy` is false while a release is outstanding; the orchestrator treats ADS-B as healthy during intentional blackouts.

## Related modules

- [Capture](capture.md) — ADSBModule, AISModule, ACARSModule.
- [SensorOrchestrator](orchestrator.md) — launches the coordinator loop and reflects its health.
- `modules/sdr_manager.py` / `sdr_utils.py` — inventory and USB-id helpers.

## Hardware notes

On the fixed node a single RTL-SDR is time-shared (`adsb:600,ais:30`) with ACARS preemption; a VHF antenna is attached via an SMA splitter. On the mobile node no SDR is present.

## See also

- [docs/design-and-roadmap.md](../design-and-roadmap.md) — original SDR pivot notes.
