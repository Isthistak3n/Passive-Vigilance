# SensorOrchestrator & PassiveVigilance

The runtime spine of Passive Vigilance. `PassiveVigilance` (in `main.py`) owns process lifecycle, mode resolution, and the high-level sensor graph. `SensorOrchestrator` (in `modules/orchestrator.py`) owns every poll loop, identity resolution, scoring injection, watchdog heartbeats, and the hand-off to durable stores and outputs.

## Overview

| Component | File | Responsibility |
|---|---|---|
| **PassiveVigilance** | `main.py` | CLI entry, env loading, mode resolution (`fixed` / `mobile`, required), sensor construction, graceful shutdown |
| **SensorOrchestrator** | `modules/orchestrator.py` | Asyncio coordination of all capture loops, GPS stamping, scoring, entity/baseline writes, alerts, GIS, GUI push |

Everything that happens at runtime eventually flows through the orchestrator.

## Design rationale

The system is deliberately single-process and asyncio-first. Capture modules are thin adapters; the orchestrator is the only place that:

- owns the event loop and the systemd watchdog,
- decides which scoring engine is live,
- injects identity and anchors before scoring,
- writes to both durable stores,
- decides what becomes an alert or a GIS feature.

This keeps the capture modules interchangeable and the scoring engines pure.

## Runtime role

### PassiveVigilance (`main.py`)

1. Loads `.env` / environment.
2. Resolves `NODE_MODE` — **required**, either `fixed` or `mobile`. It fails loud (aborts) if the value is unset or invalid rather than guessing; there is no `auto` and no silent default. (`SDR_MODE`, a separate setting, is the one that has an `auto`.)
3. Constructs the sensor graph (GPS, Kismet, optional BLE / Remote ID / ADS-B / AIS / ACARS, SDR coordinator when needed).
4. Instantiates the appropriate scoring engine (`PersistenceEngine` or `FixedScoring`).
5. Starts the orchestrator and (optionally) the Flask GUI thread.
6. Handles SIGTERM / SIGINT for clean session close (shapefile, KML, WiGLE upload, store checkpoints).

### SensorOrchestrator

The orchestrator runs a set of concurrent asyncio tasks:

- **Kismet poll loop** — the primary Wi-Fi / BT path.
- **GPS reader** — background thread; the loop only ever reads the latest fix.
- **ADS-B / AIS / ACARS / Remote ID drain loops** — when those modules are present.
- **Watchdog** — systemd notify + radio-health checks.
- **EntityStore / BaselineStore writers** — on every relevant poll.
- **Alert / GIS / GUI push** — after scoring.

#### Typical Kismet poll path

```
GPS fix (or None)
    → KismetModule.poll_devices(gps_fix=…)
    → ignore-list filter (already applied inside Kismet)
    → BLE advert buffer merge (if BLEScanner live)
    → EntityStore.record_poll (orthogonal; never affects scoring)
    → distinctive anchors from EntityStore
    → contact identity resolution (device_identity)
    → co-presence / person linking (optional)
    → ScoringEngine.update
    → DetectionEvent list
    → rate-limited alerts + GUI SSE + JSONL forensic logs
    → GIS writers on session close
```

Fixed-mode nodes also drive BaselineStore upserts inside FixedScoring; mobile nodes never open it.

## Key classes & methods

### SensorOrchestrator

- `start()` / `stop()` — lifecycle; starts the coordinator loop when in SHARED SDR mode.
- `_kismet_loop()`, `_adsb_loop()`, etc. — the concrete poll coroutines.
- `_resolve_contact`, `_update_copresence` — identity layer entry points.
- Watchdog helpers that treat intentional SDR blackouts as healthy.

### Mode resolution

`NODE_MODE` is read once at construction. Changing it via the GUI writes `.env` but still requires a service restart — the scoring engine and store graph are not hot-swappable.

## Example workflows on development hardware

### Fixed node

- Mode forced `fixed`.
- BaselineStore learns for `FIXED_BASELINE_HOURS` (default 72 h) then freezes.
- EntityStore records every audible device; distinctive anchors feed FixedScoring novelty checks.
- Single RTL-SDR time-shared (`adsb:600,ais:30` + ACARS preemption).
- Full Leaflet GUI on port 8088.

### Mobile node

- Mode `mobile`.
- PersistenceEngine only; BaselineStore never opened.
- EntityStore still records (orthogonal) so cross-session identity and contact designators continue to work.
- No SDR → ADS-B / AIS / ACARS stay dark.
- Mobile GUI (Nearby + Survey tabs).

## Configuration highlights

| Variable | Default | Notes |
|---|---|---|
| `NODE_MODE` | *required* | `fixed` / `mobile` — no default; aborts if unset or invalid |
| `KISMET_ACTIVE_WINDOW_SECONDS` | 300 | Critical for both modes |
| `ENTITY_AUDIBLE_WINDOW_SECONDS` | 0 | Tightening recommended on fixed |
| `FIXED_BASELINE_HOURS` | 72 | |
| `GUI_ENABLED` | false | |
| `SDR_CYCLE_SLICES` | adsb-only | SHARED mode |

## Pitfalls

- **Mode is not hot-swappable.** GUI toggle only updates `.env`; restart required.
- **Watchdog vs SD latency.** A slow EntityStore or BaselineStore commit on an SD card can still trip the watchdog; WAL + batching + optional async writer exist for this reason.
- **Kismet cumulative list.** Without an active window a departed device keeps being re-stamped → false followers (mobile) or hour-mask smear (fixed).
- **Orphaned SDR decoders.** The coordinator reclaims them at start; a previous crash that left dumpvdl2 holding the dongle is the classic outage.

## Related modules

- [GPSModule](gps.md)
- [Scoring Engines](scoring.md)
- [Durable Stores](stores.md)
- [Capture](capture.md)
- [Identity](identity.md)
- [SDR](sdr.md)
- [Outputs](outputs.md)
- [Recon-Pair Survey](survey.md)

## See also

- `CLAUDE.md` — module map and coding conventions
- `docs/architecture.md` — runtime shape
- `CONTEXT.md` — live node adapter maps
