# Module Reference

This directory contains Wikipedia-style reference articles for the major modules of Passive Vigilance. Pages are ordered around the runtime data flow that begins in the orchestrator.

## Reading order (orchestrator-centric)

1. **[SensorOrchestrator & PassiveVigilance](orchestrator.md)** — the central spine
2. **[GPSModule](gps.md)** — position/time backbone
3. **[Scoring Engines](scoring.md)** — mobile location-diversity & fixed baseline-deviation
4. **[Durable Stores](stores.md)** — EntityStore & BaselineStore
5. **Capture**
   - **[Capture index](capture.md)** — overview and shared contracts
   - **[KismetModule](kismet.md)** — Wi-Fi / BT via Kismet REST
   - **[BLEScanner](ble.md)** — passive raw-HCI advertisements
   - **[RemoteIDModule](remote_id.md)** — FAA ASTM F3411 via Kismet IEs
   - **[ADSBModule](adsb.md)** — readsb aircraft
   - **[AISModule](ais.md)** — marine vessels
   - **[ACARSModule](acars.md)** — aviation datalink
6. **[Identity Layer](identity.md)** — fingerprints, contact identity, co-presence, designators
7. **[SDR Coordination](sdr.md)** — single-dongle time-share
8. **[Outputs](outputs.md)** — alerts, GIS, WiGLE, GUI
9. **[Recon-Pair Survey](survey.md)** — fixed-base + mobile-patrol hunting team (optional)

## Structure of each article

- Overview
- Design rationale
- Runtime role / key classes & methods
- Workflows and example employment on development hardware
- Configuration
- Pitfalls and edge cases
- Related modules
- Hardware notes (fixed / mobile node)

These articles deliberately weave material from `CLAUDE.md`, `docs/architecture.md`, `CONTEXT.md` and the source itself into a single, orienting reference for both newcomers and subject-matter experts.

## Coverage status

The pages above cover every major runtime component and the complete data path from radio → score → store → alert → GIS, plus the optional recon-pair survey subsystem. Supporting utilities (`ignore_list`, `probe_analyzer`, `aircraft_registry`, `air_scoring`, `promotion_policy`, `sighting_rollup`, `alert_suppression`, `core/exceptions`, `core/logging`, deploy scripts, etc.) are documented inline inside the pages that consume them.

**Documentation of the core system is complete.**
