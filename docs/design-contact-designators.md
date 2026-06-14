# Design note: contact designators for WiFi/BT devices

## Why

The WiFi/BT panel lists devices by raw MAC (or fingerprint hash) — unreadable, and
impossible to *refer to* ("watch `a2:11:…`" means nothing). This tool is really a
contact tracker, so it should label devices like naval/air **track designators**: a
short, stable, human-readable name an operator can recognise across sightings and
talk about. A device a person carries rotates its MAC; its *designator* must not.

A second benefit (operator-noted): the designator's class prefix encodes the device
type, so the separate **Device** column in the table becomes redundant and is
removed — the panel reads like a track table, not a spreadsheet.

## Format

```
CLASS-IDENT-#
```

- **CLASS** — device class, from the Kismet device type:
  `Wi-Fi AP → AP`, `Wi-Fi Client → CLI`, `Wi-Fi Bridged → BR`, BTLE/Bluetooth → `BLE`;
  anything else → `DEV`. (This is what makes the Device column redundant.)
- **IDENT** — the most identifying name available, in priority order:
  1. **Network name** — the AP's broadcast SSID, or a client's probed-SSID identity
     (already surfaced as `fingerprint_label`). e.g. `NETGEAR13_5G`.
  2. **Vendor** — when there's no network name: Apple, Samsung, … (from the
     manufacturer / BLE company id).
  3. **Short token** — when there's neither: a few hex of the device's stable
     fingerprint (or the MAC tail), so every contact still gets a stable IDENT.
  Whitespace and the `-` separator are squeezed to `_` and the field is length-capped,
  so the designator stays one readable token.
- **#** — an instance number distinguishing devices that share the same
  `CLASS-IDENT` (e.g. the three radios of one mesh SSID, or three phones probing the
  same network). **Persisted and sequential**, assigned once per logical device and
  stable thereafter.

Examples: `AP-NETGEAR13_5G-1`, `AP-NETGEAR13_5G-2` (two radios of one network),
`CLI-NETGEAR13_5G-1` (a client probing it), `BLE-Apple-3`, `CLI-7a3f-1` (no name).

## Stability — the core property

A designator is bound to the device's **rotation-stable identity key** — the
`wifi-fp:` / `ble-fp:` fingerprint, or `mac:<mac>` for a stable MAC (the same key the
scorer uses, from [`modules.device_identity`](../modules/device_identity.py)). So a
device's rotating addresses keep one designator, and the **number stays put across
rotations, restarts, and sessions**. A track number that reshuffled on restart would
be worse than a raw MAC; persistence is what makes this a designator and not a
decoration.

## Where it's assigned

The number assignment is persisted in the **entity store**
([`modules.entity_store`](../modules/entity_store.py)), which already records every
device at the poll site for **both** node modes — so fixed and mobile get consistent
designators with no extra plumbing. A new table maps `identity_key → (group, number)`:

```
contact_designator(identity_key PRIMARY KEY, group_key, number, first_assigned)
```

`assign_contact_number(identity_key, group_key)` returns the existing number if the
identity is known, else `max(number for that group) + 1`, persisted. It is called
only from the asyncio poll thread (the GUI never queries it — the finished label
rides on the event), so no cross-thread locking is needed.

The label itself is built in the orchestrator when it shapes the WiFi/BT event, from
fields already on the `DetectionEvent` (class from `device_type`, IDENT from
`ssid` / `fingerprint_label` / `manufacturer`, key from `fingerprint`), and shipped
as a new `contact` field. If the entity store is absent, the number falls back to a
short stable hash of the identity key (still stable, just not clean-sequential).

## GUI

- The **Identity** column shows the designator (`contact`).
- The **Device** column is **removed** (the `CLASS` prefix carries it).
- The **SSID** column stays — it's the raw network name; the designator's IDENT is a
  derived/abbreviated token, so they complement rather than duplicate.

Scope: the fixed dashboard (`index.html`). The `contact` field rides on the event
regardless, so the mobile GUI can adopt it later.

## Honest limits

- IDENT can collide before the number disambiguates (many devices, same SSID) — that's
  what `#` is for; the number is the unique part.
- A device with no network name, no vendor, and a weak fingerprint gets a token-based
  IDENT — readable and stable, but not descriptive. That's the same floor as the rest
  of the fingerprinting work: a device that exposes nothing distinctive stays generic.
