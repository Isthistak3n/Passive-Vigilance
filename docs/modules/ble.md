# BLEScanner

Passive BLE advertisement capture that replaces Kismet’s empty `linuxbluetooth` feed. Owns the Bluetooth controller directly through a raw HCI socket.

## Overview

`modules/ble_scanner.py` listens **passively** (`scan_type=0x00` — the radio never transmits) for advertising reports and produces structured records the orchestrator can fingerprint and score.

| Responsibility | Detail |
|---|---|
| Transport | Raw HCI socket on `hci0` (or auto-detected index) |
| Mode | Passive LE scan only |
| Output | `BLEAdvert` objects → buffered per address → merged into the Kismet device list |

## Design rationale

Kismet’s Linux Bluetooth source returns empty advertisement fields and a flat `0` for signal strength on this hardware. BlueZ’s offloaded advertisement monitoring path also returns nothing on the validated controller. Raw HCI advertising reports work and carry real RSSI.

## Key behaviours

- **Passive only** — never sends SCAN_REQ.
- **Exclusive controller use** — Kismet’s Bluetooth source and `bluetoothd` must be removed/stopped.
- **Capability requirements** — `CAP_NET_RAW` + `CAP_NET_ADMIN` (granted by the service unit). Without them `connect()` degrades gracefully and the module is skipped.
- **Auto HCI index** — `BLE_HCI_DEVICE` override, else lowest present controller, so a USB dongle that re-enumerates after a reset is still found.
- **Parse depth** — manufacturer data (with stable type prefix), 16/32/128-bit service UUIDs, solicited UUIDs, local name, appearance, TX power, directed-advert flags.
- **Controller-loss handling** — on USB drop the scanner tears itself down and flips `available=False` so the orchestrator’s radio-health check can alert.

## Workflow

```
BLEScanner.connect()
  → open raw HCI socket, set passive scan parameters, enable scan
  → add_reader on the socket
  → on each advertising report → parse → on_advert callback
Orchestrator buffers latest advert per address
  → merges buffer into the Kismet device list on every poll
  → BLE records flow through the same identity / scoring / EntityStore / GUI path
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `BLE_SCANNER_ENABLED` | false | Opt-in |
| `BLE_HCI_DEVICE` | auto | `hci1` or `1` accepted |

## Pitfalls

- Leaving Kismet’s Bluetooth source or `bluetoothd` running will contend for the controller.
- Randomized addresses still dominate; a static vendor address is the only case that resolves via the offline OUI database.
- A dead HCI file descriptor that stays “readable” will spin the event loop if the reader is not removed — the teardown path exists specifically for this.

## Hardware notes

The fixed node uses a USB Bluetooth dongle on `hci0`. A mobile node can run the same stack when a dongle is present.

## Related modules

- [Capture index](capture.md)
- [Identity](identity.md) — BLE fingerprints (`ble-fp:`)
- [SensorOrchestrator](orchestrator.md) — buffers and merges adverts
