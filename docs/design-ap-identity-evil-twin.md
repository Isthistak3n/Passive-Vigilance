# Design: access-point hardware identity & evil-twin detection

Status: **direction set (2026-08-06)** — after a live feasibility pass on `chase`, the plan is
**not to build a bespoke detector**. Kismet already ships a WIDS that raises the evil-twin
signals automatically; the work is to **consume those alerts** and to **feed Kismet the one thing
it lacks** (an authorized BSSID↔SSID list, which PV already learns as its baseline). The earlier
WPS-fingerprint building block (`feat/ap-wps-identity`) stays **parked** (0% identity coverage on
`chase`). This note records the threat model, what was tried, what the live data actually showed,
and the direction it points to — so the idea and the findings are not lost.

## Why this exists

Every other identity problem in Passive Vigilance is about **clients** that randomize their
MAC. Access points are the opposite: an AP does **not** randomize its BSSID, so its MAC is
already a stable identity. The interesting adversary question flips accordingly — not *"which
rotating MACs are one device"* but *"is an AP around me claiming to be a network it isn't?"*

That is the **evil-twin**: an attacker stands up an AP advertising a trusted SSID (a home,
office, or café network) to lure client associations — to harvest traffic, run a captive
portal, or simply to know when a target is present. To a person and to most tools it looks
identical to the real network. A fixed node that has learned the real APs at a location is
precisely the right sensor to notice a second box wearing the same name — or a clone of a box
that's already there.

The fixed node answers "who is watching me"; this feature extends it to "and is one of the
networks around me an impostor."

---

# Part I — What the environment actually looks like (live findings, 2026-08-06)

Before designing a detector, we measured the real RF environment on `chase`.

## 1. The capture substrate is already live

`beacon_evidence` (BSSID / SSID / channel / crypt / beacon_count / per-AP RSSI stats) is written
on `main` by `entity_store.py` + `orchestrator.py` — ~165 APs, continuously fresh — and a
`network_affinity` table exists alongside it. So evil-twin is a **detection** problem, not a
capture one; the raw material is on disk today.

## 2. The base-rate problem is real

Many SSIDs legitimately span **multiple BSSIDs**: `Spectrum Mobile` (9), `Linksys15034` (6, plus
a `…Guest` at 3), `DIRECT-` (5–8, Wi-Fi Direct), and a long tail of mesh/extender pairs (2–4).
So the naïve rule *"a new BSSID on a known SSID is a twin"* would fire constantly on ordinary
mesh nodes, range extenders, and carrier hotspots. Any impostor-SSID detector has to survive a
high benign base rate.

## 3. A per-hardware beacon fingerprint does **not** exist here

The natural idea — a composite beacon fingerprint that **clusters by hardware** (legit mesh nodes
share it, a twin on other hardware differs) — was tested and **does not hold**. Kismet exposes
`dot11.device.beacon_fingerprint` (~90% of APs) and `dot11.advertisedssid.ietag_checksum` (100%),
but authoritative per-device reads show they are **per-BSSID unique**: 18 distinct fingerprints
across 18 APs; within a multi-BSSID SSID each BSSID carries its own fingerprint; even two BSSIDs
on the *same physical radio* (`E8:D2:FF:B2:54:FE` / `:FF`) differ. Kismet's `beacon_fingerprint`
is a **per-BSSID change detector** (its designed purpose: flag when *one* BSSID's beacon
composition changes = someone spoofing that exact AP), **not** a hardware-class identity. It can't
tell a legitimate new extender from an impostor on a shared SSID.

> Measurement gotcha for anyone re-checking: a field-limited **view** query returns a constant
> artifact for `beacon_fingerprint`. Read the full `/devices/by-key/<key>/device.json` and extract
> recursively — the field is nested; a flat `.get()` returns `None`.

## 4. `crypt` is captured by Kismet but dropped by us

`crypt` is `None` for every `beacon_evidence` row, yet Kismet's `crypt_string` is rich and always
present (`WPA3 WPA3-SAE AES-CCMP`, `WPA2-PSK`, `Open`, …). A crypto **downgrade** (WPA3/WPA2 → Open)
is one of the strongest twin tells, and we're currently discarding it. (See §III — Kismet already
alerts on this transition itself, so the value of capturing it ourselves is mostly display.)

---

# Part II — The pivot: Kismet already has a WIDS

The decisive finding. Kismet ships **50 alert definitions**, and the evil-twin-relevant ones are
already present — several firing **automatically, with no configuration**:

| Kismet alert | Sev | What it catches | Config? |
|---|---|---|---|
| **CRYPTODROP** | 15 | a previously-encrypted SSID stops advertising encryption (crypto downgrade) | auto |
| **BSSTIMESTAMP** | 10 | a BSSID's beacon timestamp jumps → two boxes claiming one BSSID = **clone** | auto |
| **BEACONRATE** | 15 | the advertised beacon rate of an SSID changes | auto |
| **CHANCHANGE** | 5 | a known AP changes channel | auto |
| **DEAUTHFLOOD / BCASTDISCON** | 10 | the deauth that usually *accompanies* an active twin (kick clients off the real AP) | auto |
| **APSPOOF** | 15 | a beacon/probe from a MAC **not** on the authorized list for an SSID (impostor-SSID) | **needs an allowlist** |

`CRYPTODROP` and `BSSTIMESTAMP` **are** the "cloned-AP" detection this note originally scoped by
hand — done automatically, internally, by the WIDS. Building a bespoke `beacon_fingerprint`-diff
engine in PV would **reimplement Kismet**. (On `chase` right now the only raised alerts are 50 ×
`NOCLIENTMFP` — sev-5 client-side noise filling a small buffer — i.e. the WIDS is live and firing,
with no actual twin/spoof events in a benign environment.)

The one gap: **APSPOOF needs configuration** — an `apspoof=` list of authorized MACs per trusted
SSID — and without it, the impostor-SSID case never fires. That gap is exactly what PV can fill.

---

# Part III — Direction

The evil-twin feature is mostly **"consume and route Kismet's existing WIDS alerts,"** plus one
custom integration where PV holds information Kismet does not.

### 1. Consume Kismet WIDS alerts (small; part of the 1.0 alerting gate)

PV already speaks Kismet's REST API (`KismetModule`). Add a poller for
`/alerts/last-time/<ts>/alerts.json` (auth = `KISMET` cookie), **filter the noise** (drop
`NOCLIENTMFP`; keep severity ≥ 10), and route the survivors through PV's existing alert backend.
This is the correct architecture — Kismet is the WIDS, PV is the sensor-fusion + alerting layer —
and it **folds directly into the last 1.0 gate (production-wired alerting)** rather than being a
separate post-1.0 feature. It also gives the alert backend *real, meaningful* events to validate
against, instead of only the randomized-MAC flood.

### 2. Feed Kismet's `APSPOOF` allowlist from PV's baseline (the unique value)

The impostor-SSID case (new BSSID on a trusted SSID) is the one neither Kismet-out-of-the-box
(needs config) nor a `beacon_fingerprint` approach (per-BSSID unique → FP-prone) handles well.
But **PV already learns the legitimate BSSID↔SSID set** during baseline. So PV can *generate*
Kismet's `apspoof=` rules from that baseline — the authorized MACs per SSID — and let Kismet do
the matching. PV supplies the ground truth Kismet is missing; Kismet raises `APSPOOF` when a beacon
claims a baseline SSID from a MAC that was never part of it. Guarded like every correlation
feature: alert-only, never touches capture or the scoring baseline, and a legitimately-added AP is
a baseline update, not a permanent twin.

### 3. (Optional) Capture `crypt_string` into `beacon_evidence`

Cheap, and useful for PV's own display/history, but **not** load-bearing for detection — Kismet's
`CRYPTODROP` already alerts on the downgrade transition itself.

### What we are explicitly **not** doing

- **Not** building a bespoke `beacon_fingerprint` / IE-diff detector — it reimplements
  `CRYPTODROP` / `BSSTIMESTAMP` / `BEACONRATE`, and the fingerprint doesn't cluster by hardware anyway.
- **Not** merging the WPS building block as the backbone (see Appendix) — 0% identity coverage on `chase`.

---

## Appendix — the parked WPS building block

Branch `feat/ap-wps-identity` (+10 tests, held from merge) captures AP-side WPS beacon attributes
(manufacturer / model / serial / device name) into a `wps-fingerprint`, with an over-merge guard
(a bare manufacturer alone yields no fingerprint). It is a genuine *hardware* descriptor and would
survive an AP changing BSSID/SSID. But a 45 h `chase` soak found **0% coverage** of the
identity-bearing fields (model/serial) — consumer APs here run WPS locked down or off — so the
fingerprint it builds is never generated. A small later sample did show a few APs (e.g. Roku)
exposing WPS manuf/model, so coverage is site-dependent and non-zero *somewhere*; if a future site
proves WPS-rich it could return as a **high-confidence booster** to `APSPOOF` (an authorized-identity
signal stronger than the MAC allowlist). Until then it stays parked. Do **not** merge WPS capture
alone into the default node — it is inert here and adds paths with nothing behind them.

## Decision

- **Now / next:** treat evil-twin as part of the 1.0 alerting work — **consume Kismet's WIDS
  alerts** (§III.1) and **auto-generate the `APSPOOF` allowlist from PV's baseline** (§III.2).
- **Optional:** capture `crypt_string` for PV's own display (§III.3).
- **Parked:** the bespoke `beacon_fingerprint` detector (redundant with the WIDS) and the WPS
  branch (0% coverage here; a booster at most).
