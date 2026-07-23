# Passive Vigilance — A Retrospective (April → July 2026)

**Purpose:** the story behind the code — how Passive Vigilance went from a bare
Raspberry Pi to a seven-sensor counter-surveillance platform — and then a two-node
hunting team — across **~12 weeks** (#1, 2026-04-13 → #204, 2026-07-06), what kept
breaking, and what we learned. This is the narrative companion to the empirical record in
[field-findings-2026-06.md](field-findings-2026-06.md) and
[field-test-2026-06.md](field-test-2026-06.md), and the forward plan in
[design-and-roadmap.md](../design-and-roadmap.md). Configuration and hardware facts
live in `CLAUDE.md` / `CONTEXT.md` and are cross-referenced, not repeated.

---

## Timeline at a glance

| Act | When | PRs (representative) | Theme |
|---|---|---|---|
| I — Foundation | Apr 13–20 | #2 #4 #5 #7 #9 #11 #13 #15 #20 #25 #30 | The whole sensor suite, stood up in two weeks |
| II — Coherence | May | #28 #29 #31 #37 #38–#47 | SDR hardening + a docs-canon reconciliation |
| III — Field reality | early Jun | #53 #54 #58 #59 #61 #63 #64 #65 | Real crashes: SDR SIGSEGV, ntfy loop, RSSI bug |
| IV — A brain | early–mid Jun | #62 #66 #67 #68 #70 #71 #74 #75 | Fixed vs. mobile; the learned baseline |
| V — Staying alive | mid Jun | #79 #85 #93 #95 #96 #97 #98 #99 | The crash-loop wars; never block the loop |
| VI — Identity | mid Jun | #88 #104–#112 #114 #117 #122 | Defeating MAC randomization by fingerprint |
| VII — Operable + proven | mid–late Jun | #113 #121 #126 #130 #136 #137 #138 #141 #143 #146 #149 #156 | Durable GUI + the multi-day soaks |
| VIII — The SDR pivot | late Jun → Jul 1 | #157 #158 #160 #161 #165 #168 #169–#172 | Retire DroneRF; AIS/ADS-B/ACARS; bring-up |
| IX — The recon pair | Jul 4–6 | #194 #195 #198 #200 #202 #203 #204 | Two nodes hunt as a team; patrols + the wardrive index |

---

## Act I — Build the whole sensor in two weeks (#1–#30)

The platform came together with startling speed. In a single mid-April sprint the
entire sensor suite went from nothing to capturing end-to-end:

- **Kismet capture + a one-command installer** (#2) — WiFi/BT via the REST API,
  with `install.sh` auto-detecting the OS.
- **ADS-B + DroneRF on the SDR** (#4) — readsb decode with adsb.lol enrichment,
  and the first pass at the drone-frequency scanner.
- **WiFi monitor mode** (#5) — udev + NetworkManager-unmanaged wiring so `wlan1`
  comes up in monitor at boot.
- **Ignore lists** (#7) — MAC/OUI/SSID filtering with a management CLI.
- **The mobile persistence engine** (#9) — time-window weighted scoring, the
  original "does this follow me?" model.
- **Pluggable alerts** (#11) — Ntfy/Telegram/Discord/Console behind one ABC.
- **CI** (#13) and a **CHANGELOG** (#19) — the project took itself seriously early.
- **Resilience** (#15) — a 5-minute health banner and sensor auto-reconnect.
- **Field hardening** (#20) — GIS-on-crash, GPS quality/HDOP filtering.
- **GPS + Kismet + logging fixes** (#24) — unblock a stalling GPS read, correct
  Kismet auth, fix log ordering.
- **The first SDR time-share** (#25) and **FAA Remote ID** decode from 802.11
  vendor IEs (#30).

By April 20 every sensor existed. What remained was making it *trustworthy*.

## Act II — Make it coherent (#28–#47)

May was consolidation. The single-dongle SDR got its first real hardening — an
exclusive handoff lock, start/stop handshakes, and backoff (#28) — the
orchestrator was split out with dedicated handoff integration tests (#29), and the
shared RTL-SDR utilities plus GPS-quality caching were extracted (#31). Release
notes landed at v0.4.3 (#37).

Then a sweeping **docs-canon** pass (#38–#47) reconciled `CLAUDE.md` /
`CONTEXT.md` / `AGENTS.md` into one home per topic, settled the branch model,
corrected the `GUI_PORT` default (5000→8080, #41), adopted a reference model for
hardware state (#43), and modernised deprecated asyncio calls (#47). This is the
discipline that keeps the living docs from lying — a habit that recurs (#155,
#167, #172) right up to the present.

## Act III — Field reality bites (#53–#65)

Then it met the real world and broke in instructive ways:

- **RSSI was always null.** Kismet returns signal under the *leaf* key, not the
  slash-path — fixed in #54, then *again* in #59 when it resurfaced live. The
  lesson is now enshrined as the "Kismet field leaf-key gotcha."
- **ntfy crash-looped** on a non-ASCII character (an em-dash) in an alert header
  (#58) — the alert path put U+2014 into a latin-1 HTTP header.
- **The DroneRF scanner SIGSEGV'd** in the Osmocom libusb stack. First mis-blamed
  on ntfy, then correctly pinned once observability was in place (#65), with a
  `DRONE_RF_ENABLED` kill-switch as the stable fallback (#64).

The supporting cast mattered too: Leaflet vendored in-repo for offline clones
(#53), a `.env.example` audit (#55), per-cycle SDR logs quieted to DEBUG so a
crash window survives in the journal (#56), the first field-test report (#60), and
persistent journald forced on despite the Raspberry Pi OS volatile-storage
override (#61) — the fix that finally *surfaced* the real SIGSEGV signal.

## Act IV — It grows a brain (#62–#76)

The conceptual leap that gave the project its identity: **fixed vs. mobile are two
different questions, not two weightings of one model** (#62). A mobile node asks
"does this device follow me across locations?"; a fixed node asks "what deviates
from this location's normal?" A fixed node run under mobile scoring never alerts
(one location cluster → every device forfeits the signal — the root of issue #50).

So `NODE_MODE` became a required, fail-loud choice forking the scoring pipeline
(#66): baseline-deviation `FixedScoring` for fixed nodes, the existing
`PersistenceEngine` for mobile. On top came a GUI mode toggle (#67), off-schedule
detection with graduated severity (#68) gated by a baseline-hours activation guard
(#69), probe-SSID + fingerprint extraction at the Kismet poll (#70), the durable
**entity/observation store** recorded at the poll site for *both* modes (#71), a
roadmap + field-findings refresh (#72/#73), endurance hardening that bounds every
detection stream for always-on running (#74), the Phase-2.5 approaching-signal
scaffolding (#75), and Bluetooth made to survive a reboot (#76). It had stopped
being a logger and become a detector with a learned pattern of life.

## Act V — The crash-loop wars (#79–#99)

Keeping it alive for days was its own campaign, and every fix taught the same
lesson: **never block the asyncio event loop, or the systemd watchdog SIGABRTs
you into a restart loop.**

- A self-healing **stall watchdog** to detect frozen capture and reconnect/restart
  (#79), refined so one reconnect fires per stall episode (#93/#94).
- The DroneRF native crash **isolated into a spawned subprocess** so a SIGSEGV
  kills only the child, not the node (#85).
- GPS reads **decoupled** from the WiFi/ADS-B pollers and hard-timeout-bounded, so
  a silent gpsd can't wedge everything (#95).
- The false-positive controls (egregious-during-baseline + randomized-MAC novelty
  anti-flood) rebuilt cleanly onto `main` (#96), and fingerprint-less randomized
  devices stopped being flagged as new (#98).
- Alert sends moved **off the event loop** onto a bounded worker, so a wedged
  backend can't restart-loop the node (#97).
- A systemd **restart-loop backstop** so a pathological churn can't run forever
  (#99).

## Act VI — Defeating MAC randomization (#88–#122)

The intellectual heart of the project. Modern phones rotate their MAC roughly
every 15 minutes, so "new MAC" is the *default* — a MAC-keyed baseline is stale
within hours (soak #1 fired novelty on ~969 devices/poll, ~60% randomized). The
insight: **the payload outlives the address.** A device rotates its MAC
aggressively but its *content* — WiFi IE structure and probe-SSID set, BLE
advertisement shape — is far more stable.

The program, built as a careful sequence: design notes (#88, #104); a passive
**raw-HCI BLE advertisement** capture spike, validated GO where BlueZ's offloaded
path returned nothing (#105); the BLE scanner module (#106) wired into the
orchestrator (#111); rotation-resistant **BLE (`ble-fp:`)** and **WiFi
(`wifi-fp:`)** fingerprints (#107/#108); keying devices by the unified fingerprint
in both modes (#109/#117); collapsing a device's rotating addresses into one
labelled identity row (#110); a thread-safe `BaselineStore` so the GUI can read
scoring state (#114); and naval-style **contact designators** (`CLASS-IDENT-#`,
#122) so an operator can actually *refer to* a contact. A crucial **over-merge
guard** keeps a device with no distinctive content (bare vendor id / no named
SSID) ungroupable, so distinct devices never fuse. On the dev node this cut the
post-freeze randomized-MAC flood from ~36/cycle to **3–5**.

A deploy scar worth remembering: #112 dropped a `CapabilityBoundingSet` that was
silently stripping the setuid caps `sudo` needs — which had broken the SDR
coordinator — and made the HCI index auto-detect so a USB re-enumeration to `hci1`
still works.

## Act VII — Operable, and proven over days (#80–#156)

Two threads braided through late June.

**The operator GUI** grew from a viewer into an instrument: a baseline-state header
and aircraft-panel fixes (#80); recent-events unified by entity identity (#101);
device type and AP-SSID/name surfaced in the WiFi/BT tab (#102/#103); a standalone
mobile GUI with a live Nearby proximity feed (#113); a three-state GPS health badge
(#120); a current-sky aircraft panel with decay (#121); a persistent aircraft log
across refresh (#123); accurate runtime sensor chiclets (#124); durable detection +
alert history that survives refresh and restart (#126/#127); the stale-cached-HTML
blank-dashboard fix (#129); sortable/filterable columns with CSV export (#141);
WiFi + aircraft persisted across refresh (#142/#144); and a live-mirror dashboard
that re-seeds on reconnect and periodically resyncs (#149).

**The soaks** became the project's conscience. Three multi-day runs each flooded in
a new way, and each flood drove a fix: egregious-during-baseline anti-flood (#96);
fingerprint-less randomized devices made novelty-ineligible (#98); the post-freeze
off-schedule flood cut (#138); and the durable answer, **rolling baseline
adaptation** with a slow-in/fast-out invariant so benign newcomers promote without
absorbing a patient adversary (#130). Soak #3 finally cleared the bar — ~42 h
post-freeze, memory bounded, 240 paged / 0 dropped (#143). The air picture matured
in parallel: the P6 current-sky panel with 24 h retention and returning-ICAO
handling (#136), then aircraft-of-interest **persistence scoring** that reframes
alerting to orbit/loiter-only instead of paging every airliner (#137). Rounding out
the phase: fingerprint enrichment with PNL + BLE reconnect signals (#146), the WiFi
identity re-keyed onto the enriched fingerprint (#151), a graceful-startup preflight
with degraded-radio alerting (#154), and egregious events made to actually *page*
during learning with a BLE-aware threshold (#156).

## Act VIII — The SDR pivot, and the July bring-up (#157–#172)

DroneRF — antenna-limited, low-value, and the source of the libusb SIGSEGV — was
**retired**, and the single radio repurposed into an ordered **AIS / ADS-B / ACARS
decode cycle** (#158 Phase 1: retire DroneRF, generalise the coordinator to an
N-band time-share, add optional AIS; #160 Phase 2: the full cycle + a GUI overhaul;
#159 docs). A brief offline-MBTiles-basemap experiment (#157) was reverted back to
plain online OSM when it proved to be scope the project didn't want (#161/#162).
Positioned aircraft were pushed to the live map even when a track wasn't advancing
(#163), and GPS reads were moved fully onto a background reader thread so the fix
never lags (#165/#166), with the weekend's work captured in docs (#167).

Then the July-1 session:
- **#168** — capture AP beacons + record network affinity (fingerprinting round 2).
- **#169** — retry the Kismet connect across the boot-readiness window, so a cold
  reboot stops silently greying out WiFi.
- **#170** — make `sensor_health` honest (a disabled sensor can't report "online")
  and relabel the misleading "Aircraft:" banner as "Sightings."
- **#171** — fix the handoff `usbreset`, which had been silently failing; now uses
  the stable VID:PID form.
- **#172** — refresh the roadmap/README to the current reality.

Alongside the PRs, **AIS went live** on a VHF antenna added to the RTL-SDR via an
SMA splitter, the node now running a 10-minute-ADS-B / 30-second-AIS cycle —
validated with **zero wedges** on the handoff, the settle barrier carrying it
cleanly. ACARS is parked pending a reception check. A small field kit (a token-gated
shutdown button + hotspot auto-join) was staged for a higher-traffic road trip.

## Act IX — The reconnaissance pair (#194–#204)

The multi-node idea that had sat in the design as "what's next" finally shipped: a
fixed base node can hand a flagged device to a roaming mobile node and ask it to
find where that device **beds down**. The mobile operator runs a **patrol** — a
bounded walk or drive that holds each target open for the whole route instead of
letting it time out on a poll quota — and along the way the node banks every access
point it hears into a **wardrive index** (#202), so a target's home network can be
matched to a location retroactively and even for a device tasked *after* the walk. A
target whose home network is never found locally is flagged as a WiGLE lookup
candidate rather than left silent.

The weekend was as much about honesty as features. A live `survkis` bring-up
surfaced that the patrol controls had only ever landed on the fixed Leaflet
dashboard — the very template a mobile node never serves — so the mobile operator
had no way to start a patrol; the controls and a Survey tab were ported onto the
mobile template they belonged on (#204). A patrol run without a GPS fix had been
silently banking nothing; it now warns the operator. The survey logic was lifted out
of the poll loop into its own `SurveyCoordinator` (#200), the WiFi alert cooldown was
re-keyed onto the contact fingerprint so a rotating address can't dodge it (#194),
and a cross-file test-isolation flake was killed so the dashboard and survey suites
pass together (#203). What it still owes is the same thing it owed on Friday: a
positive bed-down walk on the live pair.

---

## The villains that ran the whole length

- **The single-dongle SDR wedge.** From the first time-share (#25) through
  hardening (#28, #131), the libusb SIGSEGV (#63/#85), the DroneRF retirement
  (#158), to the `usbreset` fix (#171). Its cousin is **power brownout** — a noisy
  or sagging supply was blamed for the original wedge, which is why cigarette-lighter
  power on the road warrants a battery buffer.
- **Blocking the event loop.** The crash-loop teacher: a blocking prune, a slow
  alert send, or a silent gpsd read all trip the 2-minute watchdog into SIGABRT.
  Answered by #85/#95/#97/#99.
- **MAC randomization.** The problem the entire fingerprinting program (#104–#122)
  exists to beat.
- **Doc drift.** Beaten back in recurring waves (#38–#47, #155, #167, #172) so the
  living docs never lie for long.

## Lessons that hardened into rules

- **Read the leaf key, not the slash-path** (Kismet). Verify a field against the
  live daemon before building on it (#54/#59).
- **Persist the learning-window start time; never recompute on reopen** — a crash
  loop must resume the baseline, not relearn forever (#66/#74).
- **A baseline must never bake a present threat into "normal"** — hence the
  egregious-during-baseline safety net that still pages during learning (#96/#156).
- **Key on identity, not address** — the over-merge guard is non-negotiable
  (#109/#110).
- **Off the event loop** for anything that can block (#97).
- **Build offline, test, deploy once** — don't hot-edit a live soak.

## By the numbers

- **159 merged PRs** across **~11 weeks** (#1 → #172), plus the reconnaissance-pair
  arc through **#204** (early July).
- Roughly **nine arcs**: foundation, coherence, field reality, the detection brain,
  the crash-loop wars, identity, operability + soak validation, the SDR pivot, and
  the reconnaissance pair.
- **Three multi-day soaks** as the validation gate; soak #3 cleared it at ~42 h
  with bounded memory and 0 dropped alerts.
- **762 passing tests** at v0.7.0-alpha, **915** after the recon-pair arc.
- **Seven live sensors**: GPS, WiFi, BLE, ADS-B, AIS, Remote ID, and the SDR
  coordinator that time-shares the radio.

## Milestone map (capability → the PR that delivered it)

| Capability | Landed in |
|---|---|
| Kismet WiFi/BT capture + installer | #2 |
| ADS-B (readsb + adsb.lol) | #4 |
| WiFi monitor mode | #5 |
| Mobile persistence scoring | #9 |
| Pluggable alerts | #11 |
| FAA Remote ID | #30 |
| SDR single-dongle time-share (hardened) | #25 → #28 → #131 |
| Fixed vs. mobile `NODE_MODE` fork | #66 |
| Off-schedule + graduated severity | #68 / #69 |
| Durable entity/observation store | #71 |
| Endurance hardening (bounded streams) | #74 |
| Stall watchdog / crash-loop backstops | #79 / #93 / #99 |
| DroneRF crash isolation (subprocess) | #85 |
| Passive raw-HCI BLE advertisement capture | #105 / #106 / #111 |
| Rotation-resistant fingerprints (WiFi + BLE) | #107 / #108 / #109 |
| Contact designators (`CLASS-IDENT-#`) | #122 |
| Rolling baseline adaptation | #130 |
| Aircraft-of-interest persistence scoring | #137 |
| Durable GUI history + live-mirror | #126 / #127 / #149 |
| SDR pivot → AIS/ADS-B/ACARS cycle | #158 / #160 |
| Kismet boot-readiness retry | #169 |
| Honest sensor-health reporting | #170 |
| Reconnaissance pair (fixed tasks → mobile bed-down → offload) | #195 |
| Operator-bounded patrols | #198 |
| Wardrive index (retroactive bed-down) | #202 |
| Mobile-dashboard patrol controls + Survey tab | #204 |

## Where it stands (v0.7.0-alpha)

From a bare Pi to a **seven-sensor, GPS-stamped, baseline-learning
counter-surveillance platform** that now **hunts as a two-node team**: WiFi + BLE
with randomization-resistant identity and contact designators, an ADS-B/AIS
air-and-sea picture with of-interest scoring, FAA Remote ID, a durable operator
dashboard, pluggable alerting, the fixed↔mobile reconnaissance pair, and
GIS/KML/WiGLE output — behind **915 passing tests** and three multi-day soaks.

## What's next

The recon pair's remaining gate is a positive **Ph1 bed-down walk** on the live
`chase`↔`survkis` pair — the one thing the feature still owes. Beyond it: finish the
detection-quality calibration owed by P2/P3, and the identity depth that raises what
alerts *mean* — cross-session returning-entity resolution (P4) and cross-PHY
WiFi↔BLE fusion. See [design-and-roadmap.md](../design-and-roadmap.md) for the full
forward plan.

*Written 2026-07-01, at #172.*
