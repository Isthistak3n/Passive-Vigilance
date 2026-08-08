# RemoteIDModule

FAA Remote ID (ASTM F3411-22a) detection via Kismet’s device REST API. Parses Open Drone ID vendor-specific information elements that appear in 802.11 beacons.

## Overview

Kismet 2025-09 does not expose a dedicated Remote ID endpoint (its drone support is DJI DroneID only). This module therefore:

1. Polls `/devices/last-time/{ts}/devices.json` requesting the raw IE tag list and content.
2. Filters for Vendor Specific IE (tag 221) with OUI `FA:0B:BC` and vendor type `0x0D`.
3. Parses the 25-byte ASTM message units with `struct`.

Supported message types: Basic ID (0), Location/Vector (1), System (4), Operator ID (5).

## Key behaviours

- One in-memory event per UAS ID with a thinned flight-path track.
- Extracts UAS ID, UA type, status, drone lat/lon/alt, speed, heading, operator lat/lon and operator ID.
- Alerts are rate-limited per UAS ID.
- GPS stamp comes from the observer (the node’s own fix), not from the drone.

## Workflow

```
RemoteIDModule.poll()
  → POST last-time devices with IE fields
  → walk IE tag sequence for FA:0B:BC / 0x0D
  → parse 25-byte message units
  → merge into a detection dict
  → orchestrator receives list of remote_id events
```

## Configuration

Uses the same `KISMET_*` credentials as KismetModule. No additional enable flag — the module is always attempted when Kismet is up.

## Pitfalls

- IE content may arrive as hex or base64; both are tried.
- Invalid altitude / lat-lon sentinels (`0xFFFF`, `0x7FFFFFFF`) are mapped to `None`.
- Only Wi-Fi transport is currently parsed; Bluetooth Remote ID is out of scope for this module.

## Related modules

- [KismetModule](kismet.md) — shares the Kismet session credentials.
- [SensorOrchestrator](orchestrator.md) — drains and alerts on the events.
- [Outputs](outputs.md) — GIS and alert paths for drone detections.
