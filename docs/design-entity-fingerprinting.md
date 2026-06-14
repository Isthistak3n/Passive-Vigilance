# Design note: Randomization-resistant fingerprinting for entity resolution (P4)

**Status:** Design note — input to roadmap **P4 (cross-session entity resolution)**
and design §Phase F. Written 2026-06-10, motivated directly by soak #1's
post-freeze novelty flood.
**Companion:** [roadmap-fixed-node-prototype.md](roadmap-fixed-node-prototype.md),
[design-detection-modes.md](design-detection-modes.md),
[design-ble-advertisement-capture.md](design-ble-advertisement-capture.md).

> **Update (2026-06-14) — the within-session half is SHIPPED and deployed.** The
> capture prerequisite is met (passive BLE raw-HCI advertisement capture with real
> RSSI) and the per-modality signatures + keying are live: `wifi-fp:` (probed SSIDs
> + IE-set hash) and `ble-fp:` (vendor/services/name) via
> [`modules.device_identity`](../modules/device_identity.py), keyed into both fixed
> and mobile scoring. This collapses a device's rotating MACs to one identity
> *within a session* and cut the flood ~36→3–5 flags/cycle on chase. **What remains
> for P4** is the *cross-session* pass below: linking these fingerprints into stable
> entities across days and emitting the "returning entity" signal.

---

## The problem MAC randomization creates

A fixed node keyed on **MAC address** has a stale baseline within hours. Modern
phones, watches, and wearables rotate their MAC roughly every ~15 minutes, so a
device that was in the frozen baseline reappears under a new address and reads as
**brand new**. Soak #1 made this concrete: post-freeze, novelty fired on ~969
devices per poll (~10.7k alerts), and **~60% of them were randomized MACs** —
overwhelmingly baselined devices that had simply rotated.

The fingerprint keying we already have (`fp:<probe-SSID set>`) rescues only the
~quarter of clients that broadcast *named* probe SSIDs. The other ~three quarters
fall back to per-MAC and flood. **We need a correlator that survives MAC rotation
for the silent majority.**

## The insight: the payload outlives the address

A privacy-conscious device rotates its *address* aggressively, but the *content*
it transmits — the structure of its WiFi management/probe frames, the shape of its
BLE advertisements — is far more stable. That payload is a fingerprint that
persists across rotations. This is well-established (802.11 probe-request
fingerprinting; BLE / Apple-Continuity tracking despite address randomization).
**Capturing the payload and keying on it, not the MAC, is the durable fix.**

## WiFi — what is stable across a MAC rotation

A client's probe requests and a device's management frames carry, beyond the MAC:

- **Vendor-specific information elements** (OUI + vendor data) — distinctive per
  chipset/OS, often constant across rotations.
- **Supported / extended rate sets** and **HT / VHT / HE capability** fields —
  reflect the radio, not the identity, so they're stable.
- **The set and ordering of information elements** in a probe request — a known
  fingerprint surface even when SSIDs are absent.
- **The probe-SSID set** (already used) and Kismet's **`probe_fingerprint`**.

Combined, these form a per-device signature. Caveats: some stacks now randomize IE
ordering or send minimal "broadcast-only" probes; infrastructure APs are stable
regardless (they don't randomize) so they need no special handling.

## BLE — what is stable across an address rotation

BLE advertisements carry, beyond the rotating address:

- **Manufacturer-specific data** (the company ID + payload) — e.g. Apple
  Continuity, fitness-band vendor data — frequently constant across rotations.
- **Service UUIDs**, **appearance**, **advertised name**, **TX-power level**.

These persist across address randomization and are the documented basis for BLE
tracking. Two project-specific notes:
- Recon found this node's *stable-identity* BLE subset skews to fixed **appliances**
  (TVs, etc.), so BLE-as-identity is weaker here than WiFi — but the *payload
  cluster* of a person's wearables is still a usable correlator.
- BLE is short-range (~10 m), so a BLE detection is also a **proximity** signal in
  its own right — see the companion proximity/person-presence reasoning.

## Cross-modal fusion: resolve the person, not the device

A human carries WiFi and BLE devices that beacon **together**. A co-occurring
WiFi-fingerprint plus BLE wearable-cluster, appearing at the same times, is a
**"person" entity** far more stable than any single radio — and re-appearance of
that pair is a **returning person**. This is the counter-surveillance payoff:
"is this the same someone who was here yesterday, or cased me last week."

## How it lands in the entity store

The `entity_store` already has the right tables — `probe_evidence`,
`device_fingerprint`, `entities`, `observations` — and P4 was always "merge
fingerprints / probe evidence into stable entities." This note specifies the
*fingerprint content* and a resolution pass that:

1. computes a per-device signature from the captured IE/advertisement payload;
2. links a device's rotating MACs to **one logical entity** via signature match
   (union-find over shared fingerprints, as the probe-SSID grouping already does);
3. surfaces **"returning entity"** as a first-class signal.

## Capture requirements — MET (2026-06)

Originally we polled only **device-level summaries** from Kismet. Fingerprinting
needed the raw payload; that capture work is now done:

- **WiFi:** ✅ Kismet's `probe_fingerprint` (the IE-set hash) is folded into the
  `wifi-fp:` signature alongside the named probe SSIDs ([`modules.wifi_fingerprint`](../modules/wifi_fingerprint.py)).
- **BLE:** ✅ a passive raw-HCI scanner ([`modules.ble_scanner`](../modules/ble_scanner.py))
  reads LE Advertising Reports directly — manufacturer/company ids, service UUIDs,
  service data, name **and a real per-advert RSSI** (Kismet's BLE feed reported a
  flat `0`). BlueZ's offloaded advertisement-monitor path didn't work on this
  controller; raw HCI is the production primitive. Restores the proximity/approaching
  signals for BLE.
- **Store:** the rotation-stable fingerprint is the scoring key; the entity store's
  `device_fingerprint`/`contact_designator` tables persist per-device evidence and a
  stable contact number. The remaining piece is keying the *cross-session* entity
  resolution by that signature (below).

## Signals this unlocks

- **Randomization-proof novelty** — flag a genuinely new *fingerprint*, not a new
  MAC. This attacks the root cause of soak #1's flood directly (and is the durable
  partner to the sustained-presence guard shipped as a stop-gap).
- **Returning entity** — cross-session re-identification, the core P4 value.
- **Person-presence** — the cross-modal WiFi+BLE cluster.

## Honest limits

- A sophisticated adversary can strip or spoof distinctive IEs / advertisement
  fields; this raises the bar, it does not make a deliberately untraceable device
  visible.
- Not every device is distinctive — minimal-probe clients and privacy-hardened
  stacks fingerprint weakly.
- BLE's stable subset here is appliance-heavy; expect WiFi to carry most of the
  signal, BLE to corroborate and add proximity.

## Phasing

1. **Capture** WiFi IEs + BLE advertisement payload + BLE RSSI (the prerequisite).
2. **Fingerprint** per modality; key `device_fingerprint` by the signature.
3. **Resolve** — the entity pass that links rotating MACs to one entity and emits
   the returning-entity signal.
4. **Fuse** WiFi + BLE into person-level entities.

**Validate on chase:** a known device must re-identify across a MAC rotation and
across a service restart; distinct devices must not merge; and the post-freeze
novelty false-positive rate must drop versus the MAC-keyed baseline.
