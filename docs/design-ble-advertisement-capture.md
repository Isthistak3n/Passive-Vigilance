# Design note: capturing BLE advertisements to fingerprint randomized-MAC devices

This note resolves the open prerequisite from
[`design-entity-fingerprinting.md`](design-entity-fingerprinting.md): that note
specifies *what* a BLE fingerprint is and *why* it survives address rotation, but
lists "capture the advertisement payload" as work we do not yet do. This is the
*how* — grounded in what the hardware on a fixed node can actually deliver — plus
how the captured data decides whether a device belongs in the detection area.

## The goal in plain terms

Modern phones and wearables rotate their Bluetooth address every few minutes, so
we cannot recognise a device by its address. But the *content* a device
broadcasts — which vendor it is, which services it offers, its transmit power —
changes far more slowly than the address. If we capture that content we can give
each device a stable "fingerprint," group its rotating addresses into one logical
device, and then ask the question that matters: **is this a device we have always
seen here, or a new one that has started loitering in range?**

## What we found on the node (live, 2026-06-13)

The current Bluetooth path gives us almost nothing to fingerprint with. Kismet
captures Bluetooth through its `linuxbluetooth` source on the single USB dongle,
and for every BLE device the advertisement fields come back empty: no service
identifiers, no vendor data, no transmit power, and a signal strength reading of
zero. We are seeing only the bare rotating address and the label "BTLE." That is
expected — Kismet's Bluetooth source reports device-level summaries from the
controller's discovery, not the raw advertisement packets that carry the
fingerprint material.

The hardware reality that shapes the options:

- **One Bluetooth adapter** (an Edimax USB dongle), currently held exclusively by
  Kismet.
- **The Linux Bluetooth daemon is switched off and disabled** — Kismet drives the
  controller directly, and its capture explicitly does not want the daemon
  running. So any approach that relies on the daemon cannot share that adapter
  with Kismet.
- **USB capacity is nearly full** — the SDR, the WiFi monitor dongle, the
  Bluetooth dongle, and a hub already occupy the bus.

A hard constraint from the platform's charter: this is a **passive** sensor that
does not transmit. Standard "active" Bluetooth scanning asks each device for more
data, which means transmitting. We must use **passive (listen-only) scanning**.
That captures the primary advertisement payload but not the secondary "scan
response," so a small amount of fingerprint material that only appears in scan
responses is out of reach by design. This is an accepted limit, not a defect.

## The options for actually capturing the payload

**A. Repurpose the existing dongle for a purpose-built advertisement listener.**
Run the Linux Bluetooth daemon on the current adapter and listen passively for
advertisement reports, which expose vendor data, service identifiers, transmit
power, and — importantly — a real per-advertisement signal strength. The cost is
that Kismet can no longer use that adapter for Bluetooth. Given that Kismet's
Bluetooth capture currently yields essentially nothing, this trades a near-empty
feed for a rich one with **no new hardware**. This is the recommended first step.

**B. Add a second Bluetooth dongle dedicated to advertisement listening.** Keep
Kismet on the existing adapter and run the listener on a second one. This avoids
giving anything up, at the cost of a cheap dongle and one of the remaining USB
ports. Worth doing only if we find Kismet's Bluetooth feed is carrying something
we still want.

**C. Add a dedicated sniffer (Ubertooth or an nRF radio).** This captures raw
advertising across all three advertising channels and is the highest-fidelity
option — it sees devices the controller path misses. Kismet can ingest an
Ubertooth directly. The cost is buying hardware, another USB port, and more setup.
This is the fidelity upgrade to reach for if the software path proves too lossy.

**D. Reconfigure Kismet's Bluetooth source.** The cheapest outcome if it works,
but the live evidence is that the current source returns empty advertisement
fields, so this would be an investigation with an uncertain payoff. Worth a brief
look before committing to A, not worth betting the feature on.

**Recommendation:** start with **A** — repurpose the existing dongle — because it
costs nothing and replaces a feed that is already empty. Keep **C** (a dedicated
sniffer) as the known upgrade path if passive controller-level capture turns out
to miss too much. Re-evaluate **B** only if Kismet's Bluetooth feed earns its keep.

## What the fingerprint is made of

From each passively captured advertisement we build a signature from the parts
that stay constant while the address rotates: the **vendor identifier(s)** in the
manufacturer data, the set of **service identifiers** advertised, any
**service-data identifiers** (for example a beacon's namespace), the device's
**appearance** and **advertised name** when present, and a coarse **transmit-power
bucket**. We deliberately exclude the parts of some vendors' payloads that
themselves rotate (Apple's, notably), so the signature does not churn with the
address. Devices that advertise nothing distinctive will fingerprint weakly — an
honest limit carried over from the parent design note.

This signature becomes the key under which a device is stored, with its observed
rotating addresses recorded as evidence beneath it — exactly the structure the
entity store was built for. It also fills a real gap in today's code: randomized
devices are currently grouped by their WiFi probe names, which Bluetooth devices
do not have, so randomized BLE devices cannot be grouped at all right now. An
advertisement signature gives them their own grouping key.

## How it decides "belongs here or not"

The fingerprint plugs straight into the existing fixed-node scoring model, which
already learns a baseline and then flags deviations:

- **During the learning window**, bank the set of advertisement fingerprints seen
  in range as part of the normal environment.
- **After the baseline freezes**, a fingerprint that is *not* in the baseline and
  then *persists* is flagged as novel — a new Bluetooth device that has started
  loitering. Because the flag is on the fingerprint, not the address, rotating the
  address no longer evades it. This attacks the same root cause as the earlier
  randomized-MAC alert floods.
- A **known** fingerprint seen at an hour it was never seen during learning is
  off-schedule, the same as for WiFi.
- Because passive advertisement capture restores a **real signal strength**, a
  Bluetooth device also inherits the approaching-signal trend work — a known or
  unknown device whose signal is steadily strengthening is closing distance.

Bluetooth's short range (~10 m) means any Bluetooth detection is also a
proximity signal in its own right, which makes a strengthening-signal alert
especially meaningful here.

## How it would show in the GUI

The WiFi/BT tab already distinguishes device type and address type. On top of
that, rotating Bluetooth addresses that share one fingerprint should collapse to a
**single logical row** rather than spraying a new row per address, labelled with a
human-readable identity derived from the advertisement (for example the vendor, or
"beacon" with its namespace), and marked as either part of the learned baseline or
newly novel. That row is the answer to "is this one device or many, and does it
belong here."

## Phasing

1. **Spike the capture (Option A).** Stand up passive advertisement listening on
   the existing dongle in place of Kismet's Bluetooth source; confirm we get
   vendor data, service identifiers, and a real per-advertisement signal strength.
   This is the go/no-go for the software path before any scoring work.
2. **Fingerprint and store.** Build the signature, key the device store by it, and
   record the rotating addresses as evidence.
3. **Score.** Extend the fixed-node device key so randomized Bluetooth devices key
   by their advertisement fingerprint, and let baseline novelty / off-schedule /
   approaching-signal apply to them.
4. **Display.** Collapse rotating addresses to one logical row in the WiFi/BT tab
   with its identity label and baseline-vs-novel state.

**Validate on the fixed node:** a known wearable must re-identify across an
address rotation and across a service restart; two distinct devices must not merge
into one fingerprint; and the post-freeze false-alarm rate for Bluetooth must drop
versus today's address-keyed behaviour.

## Honest limits

- Passive scanning cannot see scan-response-only fields; a determined device can
  also strip or vary its advertisement content. This raises the bar; it does not
  make a device that is trying to be invisible visible.
- The stable-identity Bluetooth subset on this node skews to fixed appliances, so
  expect WiFi to carry most of the identity signal and Bluetooth to corroborate
  and to add proximity — consistent with the parent design note.
