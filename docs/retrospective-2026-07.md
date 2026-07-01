# Passive Vigilance — A Retrospective (April → July 2026)

**Purpose:** the story behind the code — how Passive Vigilance went from a bare
Raspberry Pi to a seven-sensor counter-surveillance platform across **159 merged
PRs in ~11 weeks** (#1, 2026-04-13 → #172, 2026-07-01), what kept breaking, and
what we learned. This is the narrative companion to the empirical record in
[field-findings-2026-06.md](field-findings-2026-06.md) and
[field-test-2026-06.md](field-test-2026-06.md), and the forward plan in
[design-and-roadmap.md](design-and-roadmap.md). Configuration and hardware facts
live in `CLAUDE.md` / `CONTEXT.md` and are cross-referenced, not repeated.

---

## Timeline at a glance

| Act | When | PRs (representative) | Theme |
|---|---|---|---|
| I — Foundation | Apr 13–20 | #2 #4 #5 #7 #9 #11 #13 #15 #20 #25 #30 | The whole sensor suite, stood up in two weeks |
| II — Coherence | May | #28 #29 #31 #37 #38–#47 | SDR hardening + a docs-canon reconciliation |
| III — Field reality | early Jun | #53 #54 #58 #59 #63 #64 #65 | Real crashes: SDR SIGSEGV, ntfy loop, RSSI bug |
| IV — A brain | early–mid Jun | #62 #66 #67 #68 #70 #71 #74 #75 | Fixed vs. mobile; the learned baseline |
| V — Staying alive | mid Jun | #79 #85 #93 #95 #97 #99 | The crash-loop wars; never block the loop |
| VI — Identity | mid Jun | #88 #104–#112 #117 #122 | Defeating MAC randomization by fingerprint |
| VII — Operable + proven | mid–late Jun | #113 #121 #126 #130 #138 #141 #143 #146 #149 | Durable GUI + the multi-day soaks |
| VIII — The SDR pivot | late Jun → Jul 1 | #157 #158 #160 #161 #165 #168 #169–#172 | Retire DroneRF; AIS/ADS-B/ACARS; bring-up |

---

## Act I — Build the whole sensor in two weeks (#1–#30)

The platform came together with startling speed. In a single mid-April sprint:
Kismet capture with a one-command installer (#2); ADS-B via readsb plus adsb.lol
enrichment, and the DroneRF scanner, both on the SDR (#4); WiFi monitor mode with
udev + NetworkManager-unmanaged wiring (#5); MAC/OUI/SSID ignore lists with a CLU
(#7); the mobile **persistence engine** with time-window scoring (#9); a pluggable
alert layer (Ntfy/Telegram/Discord/Console, #11); CI (#13); a 5-minute health
banner with sensor auto-reconnect (#15); field hardening — GIS-on-crash, GPS
quality/HDOP filtering (#20); and FAA **Remote ID** decode from 802.11 vendor IEs
(#30). By April 20 the entire sensor suite existed and captured end-to-end.

## Act II — Make it coherent (#28–#47)

May was consolidation. The single-dongle SDR got its first real hardening — an
exclusive handoff lock, start/stop handshakes, and backoff (#28) — and the
orchestrator was split out with dedicated handoff integration tests (#29). Then a
sweeping **docs-canon** pass (#38–#47) reconciled `CLAUDE.md` / `CONTEXT.md` /
`AGENTS.md` into one home per topic, settled the branch model, corrected the
`GUI_PORT` default (5000→8080, #41), and modernised deprecated asyncio calls
(#47). This is the discipline that kept the living docs from lying — a habit that
recurs (#155, #167, #172) right up to the present.

## Act III — Field reality bites (#53–#65)

Then it met the real world and broke in instructive ways:

- **RSSI was always null.** Kismet returns signal under the *leaf* key, not the
  slash-path — fixed in #54, then *again* in #59 when it resurfaced. The lesson is
  now enshrined as the "Kismet field leaf-key gotcha."
- **ntfy crash-looped** on a non-ASCII character in an alert header (#58).
- **The DroneRF scanner SIGSEGV'd** in the Osmocom libusb stack. First
  mis-blamed on ntfy, then correctly pinned (#65), with a `DRONE_RF_ENABLED`
  kill-switch as the stable fallback (#64). Foreshadowing: the SDR would remain
  the project's most persistent adversary.

Supporting fixes landed alongside: Leaflet vendored in-repo for offline clones
(#53), a `.env.example` audit (#55), per-cycle logs quieted so the journal
survives a crash (#56), and persistent journald on the Pi (#61).

## Act IV — It grows a brain (#62–#75)

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
**entity/observation store** recorded at the poll site for *both* modes (#71),
endurance hardening that bounds every detection stream for always-on running
(#74), and the Phase-2.5 approaching-signal scaffolding (#75). It had stopped
being a logger and become a detector with a learned pattern of life.

## Act V — The crash-loop wars (#79–#99)

Keeping it alive for days was its own campaign, and every fix taught the same
lesson: **never block the asyncio event loop, or the systemd watchdog SIGABRTs
you into a restart loop.**

- A self-healing **stall watchdog** to detect frozen capture and reconnect/restart
  (#79, refined in #93/#94).
- The DroneRF native crash **isolated into a spawned subprocess** so a SIGSEGV
  kills only the child, not the node (#85).
- GPS reads **decoupled** from the WiFi/ADS-B pollers and hard-timeout-bounded, so
  a silent gpsd can't wedge everything (#95).
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

The program, built as a careful sequence: a design note (#88, #104); a passive
**raw-HCI BLE advertisement** capture spike validated GO where BlueZ's offloaded
path returned nothing (#105); the BLE scanner module (#106) wired into the
orchestrator (#111); rotation-resistant **BLE (`ble-fp:`)** and **WiFi
(`wifi-fp:`)** fingerprints (#107/#108); keying devices by the unified fingerprint
in both modes (#109/#117); collapsing a device's rotating addresses into one
labelled identity row (#110); and naval-style **contact designators**
(`CLASS-IDENT-#`, #122) so an operator can actually *refer to* a contact. A
crucial **over-merge guard** keeps a device with no distinctive content
(bare vendor id / no named SSID) ungroupable, so distinct devices never fuse. On
chase this cut the post-freeze randomized-MAC flood from ~36/cycle to **3–5**.

A deploy scar worth remembering: #112 dropped a `CapabilityBoundingSet` that was
silently stripping the setuid caps `sudo` needs — which had broken the SDR
coordinator.

## Act VII — Operable, and proven over days (#113–#156)

Two threads braided through late June.

**The operator GUI** grew from a viewer into an instrument: durable detection +
alert history that survives refresh and restart (#126/#127), a current-sky
aircraft panel with decay (#121), sortable/filterable columns with CSV export
(#141), a live-mirror dashboard that re-seeds on reconnect (#149), a
three-state GPS health badge (#120), and a standalone mobile GUI with a live
Nearby proximity feed (#113).

**The soaks** became the project's conscience. Three multi-day runs each flooded
in a new way, and each flood drove a fix: egregious-during-baseline anti-flood
(#96); fingerprint-less randomized devices made novelty-ineligible (#98); the
post-freeze off-schedule flood cut (#138); and the durable answer, **rolling
baseline adaptation** with a slow-in/fast-out invariant so benign newcomers
promote without absorbing a patient adversary (#130). Soak #3 finally cleared the
bar — ~42 h post-freeze, memory bounded, **240 paged / 0 dropped** (#143). The
air picture matured in parallel: aircraft-of-interest **persistence scoring** that
reframes alerting to orbit/loiter-only instead of paging every airliner (#137),
built on a pure geometry/scoring core.

## Act VIII — The SDR pivot, and the July bring-up (#157–#172)

DroneRF — antenna-limited, low-value, and the source of the libusb SIGSEGV — was
**retired**, and the single radio repurposed into an ordered **AIS / ADS-B /
ACARS decode cycle** (#158 Phase 1, #160 Phase 2 + GUI overhaul, #159 docs),
generalising the coordinator into an N-band time-share with a settle barrier and
sudoers-scoped service handoffs. A brief offline-MBTiles-basemap experiment (#157)
was reverted back to plain online OSM when it proved to be scope the project
didn't want (#161/#162). GPS reads were moved fully onto a background reader
thread so the fix never lags (#165/#166).

Then the July-1 session:
- **#168** — capture AP beacons + record network affinity (fingerprinting round 2).
- **#169** — retry the Kismet connect across the boot-readiness window, so a cold
  reboot stops silently greying out WiFi.
- **#170** — make `sensor_health` honest (a disabled sensor can't report "online")
  and relabel the misleading "Aircraft:" banner as "Sightings."
- **#171** — fix the handoff `usbreset`, which had been silently failing on a
  `/dev/bus/usb` path the tool rejects; now uses the VID:PID.
- **#172** — refresh the roadmap/README to the current reality.

Alongside the PRs, **AIS went live**: a VHF antenna was added to the RTL-SDR via
an SMA splitter, and the node now runs `adsb:600,ais:30` — validated with **zero
wedges** on the handoff, the settle barrier carrying it cleanly. ACARS is parked
pending a reception check. A field kit (token-gated iPad shutdown button + hotspot
auto-join) was staged for a higher-traffic road trip.

---

## The villains that ran the whole length

- **The single-dongle SDR wedge.** From the first time-share (#25) through
  hardening (#28, #131), the libusb SIGSEGV (#63/#85), the DroneRF retirement
  (#158), to the `usbreset` fix (#171). Its cousin is **power brownout** — the
  `.env` history blames USB3 noise / a sagging supply for the original wedge,
  which is why cigarette-lighter power on the road warrants a battery buffer.
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

## Where it stands (v0.7.0-alpha)

A bare Pi became a **seven-sensor, GPS-stamped, baseline-learning
counter-surveillance platform**: WiFi + BLE with randomization-resistant identity
and contact designators, an ADS-B/AIS air-and-sea picture with of-interest
scoring, FAA Remote ID, a durable operator dashboard, pluggable alerting, and
GIS/KML/WiGLE output — behind **762 passing tests** and three multi-day soaks.

## What's next

The chapters practically write themselves: confirm AIS reception from a real
vantage point; decide the ACARS decoder (acarsdec vs. dumpvdl2) once VHF viability
is proven; finish the detection-quality calibration owed by P2/P3; and the
identity depth that raises what alerts *mean* — cross-session returning-entity
resolution (P4) and cross-PHY WiFi↔BLE fusion. See
[design-and-roadmap.md](design-and-roadmap.md) for the full forward plan.

*Written 2026-07-01, at #172.*
