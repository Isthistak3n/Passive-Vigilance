# Identity Layer

The identity layer turns rotating MAC addresses into stable, human-readable contacts so the GUI, alerts and survey system can treat “one logical device” as a single entity across address changes, sessions and even physical layers (Wi-Fi + BLE).

## Overview

| Module | Responsibility |
|---|---|
| `device_identity.py` | Strong / medium / weak contact identity; shared by both scoring engines |
| `wifi_fingerprint.py` | IE-hash + rarest distinctive probe SSID → `wifi-fp:` key |
| `ble_fingerprint.py` | Vendor / services / name / mfg structures → `ble-fp:` key |
| `mac_utils.py` | Randomization detection, OUI lookup, normalize, group-by-fingerprint |
| `copresence.py` | Cross-PHY person linking (Wi-Fi client + BLE that travel together) |
| `contact_designator.py` | CLASS-IDENT-# naval-style labels |

## Design rationale

Modern devices rotate their MAC addresses. A naïve “new MAC = new device” model therefore produces both false novelty (fixed mode) and false “following” clusters (mobile mode). The identity layer solves this by keying on *content that changes slowly*:

- Wi-Fi clients → probed SSIDs + information-element hash, optionally anchored on a rare private SSID.
- BLE advertisers → company IDs, service UUIDs, local name, manufacturer-specific structures.

A fingerprint is only considered **strong** when it contains a real discriminator. Bare vendor IDs or purely public SSIDs stay weak and fall back to the MAC, so distinct devices are never merged.

Display identity is deliberately more inclusive than scoring identity (medium tier) so a returning randomized device re-links in the GUI even when its content is too weak to score on.

## device_identity

- `strong_fingerprint(device)` → `wifi-fp:…` / `ble-fp:…` or `None`.
- `contact_identity(device)` → `(key, confidence)` where confidence is `strong` / `medium` / `weak`.
- `fingerprint_label(device)` → human-readable string for the collapsed GUI row.

The orchestrator attaches `fp_anchor` (strong) and `fp_anchor_medium` (looser rarity bar) from the EntityStore before calling these helpers.

## Co-presence (person linking)

`CoPresenceLinker` groups mobile radios (Wi-Fi clients + BLE; APs excluded) that appear in nearly the same polls. Guards against over-merge include:

- Minimum co-presence count and Jaccard similarity.
- Transience / fixture fraction.
- Optional RSSI-motion correlation gate.

Links are persisted in EntityStore so a returning pair re-links immediately. The feature is off via `CROSS_PHY_LINKING_ENABLED=false`.

## Contact designators

`CLASS-IDENT-#` labels (e.g. `CLIENT-APPLE-3`) are stable across rotations because the instance number is persisted against the identity key, not the MAC.

## Pitfalls

- A randomized device with no strong fingerprint is un-trackable by design; proximity signals remain the only way it can still page.
- Medium anchors are display-only; they never affect the scoring key.
- Person linking is conservative — bias is toward a missed link rather than a false merge.

## Related modules

- [Scoring Engines](scoring.md) — use the strong fingerprint for keying.
- [Durable Stores](stores.md) — supply anchors, PNL, contact registry and designator numbers.
- [Capture](capture.md) — produce the raw records that are fingerprinted.

## See also

- [SensorOrchestrator](orchestrator.md) — `_resolve_contact`, `_update_copresence`, resident/visitor classification.
