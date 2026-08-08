# Capture Modules

The capture layer turns raw RF into structured, GPS-stamped device records that the orchestrator can score, store and display. All modules follow the same lifecycle (`connect` / `poll` or `drain` / `close`) and degrade gracefully when hardware is absent.

## Module pages

| Module | Page | Source | Default |
|---|---|---|---|
| **KismetModule** | [kismet.md](kismet.md) | Kismet REST API | Always on |
| **BLEScanner** | [ble.md](ble.md) | Raw HCI | Opt-in (`BLE_SCANNER_ENABLED`) |
| **RemoteIDModule** | [remote_id.md](remote_id.md) | Kismet 802.11 vendor IE | Always attempted |
| **ADSBModule** | [adsb.md](adsb.md) | readsb JSON | Always on when SDR present |
| **AISModule** | [ais.md](ais.md) | AIS-catcher UDP JSON | Opt-in (`AIS_ENABLED`) |
| **ACARSModule** | [acars.md](acars.md) | acarsdec / dumpvdl2 UDP JSON | Opt-in (`ACARS_ENABLED`) |

## Shared contracts

- Capture modules never open gpsd themselves; the orchestrator supplies the current fix.
- SDR bands (ADS-B, AIS, ACARS) expose `can_scan` / `auto_disabled` so the coordinator and GUI treat them uniformly.
- Drain-style modules (`drain_detections`) return and clear a buffer; poll-style modules return a fresh list each call.
- Failures are swallowed or raised as `ConnectionError` so the orchestrator can reconnect; they never crash the process.

## Hardware notes

| Node | Capture stack |
|---|---|
| **chase** | wlan1 (monitor) + USB BT dongle + RTL-SDR (ADS-B/AIS/ACARS time-share) + Remote ID via Kismet |
| **survkis** | wlan1 (monitor) + u-blox GPS; no SDR → ADS-B/AIS/ACARS stay dark |

## See also

- [SensorOrchestrator](orchestrator.md)
- [SDR Coordinator](sdr.md)
- [Identity Layer](identity.md)
- [docs/setup.md](../setup.md)
