# Design: the reconnaissance pair — fixed tasks, mobile surveys

Status: **shipped in PR #195** (`SURVEY_ENABLED`, default off) — owes the on-node
validation in Part II below. This is the multi-node correlation named in
`design-and-roadmap.md` §5.5 ("a follower seen on the mobile node now appearing at the
fixed node") and previously deferred; roadmap §12 is now a pointer to this document.

## Why this exists

The fixed and mobile threat models are complementary halves of one workflow. A **fixed**
node learns a place and surfaces a suspicious device — but it can only say *"this device
is here and behaving oddly,"* never *where it lives.* A **mobile** node's native strength
is location diversity ("where does this go?"). Pair them: the fixed node **tasks** the
mobile node to go find where a flagged device **beds down** (its residence), and the survey
data flows back. The stationary node answers "who is watching me"; the mobile node answers
"and where do they come from."

---

# Part I — Design

## 1. The enabler: portable identity

A tasking has to name a device in a way a *different* node can recognise. Raw MACs rotate
every ~15 min, so they are useless across nodes. But the content identity keys
(`wifi-fp:` / `ble-fp:`, from `modules/device_identity.py`) are **content-derived and
deterministic** — two nodes observing the same device compute the same key.

- **BLE** keys are pure advertisement content → directly portable.
- **WiFi** keys are the IE-set hash anchored on *one* distinctive probed SSID, and which
  SSID a node picks depends on its own local SSID rarity — so the mobile matcher does **not**
  assume a shared anchor. It tests whether an observed device *could produce* the tasked key
  by trying each of the device's own probed SSIDs as the anchor
  (`SensorOrchestrator._device_candidate_keys`). The tasked key falls out whenever the mobile
  node sees the device probe the SSID the fixed node anchored on.

A bare `mac:` contact (no distinctive content) has **no portable key and is not surveyable** —
the button and the endpoint refuse it rather than issue a task that could never match.

## 2. Topology: store-and-forward over the base LAN

The fixed node is the always-on base station and already runs the authenticated Flask GUI, so
it hosts the exchange. The mobile node is offline while surveying and syncs only when back in
range of base:

- `GET /api/tasking` — pull the current watchlist.
- `POST /api/survey` — offload the computed survey result.

Both are **token-gated control actions** (refused when no `GUI_TOKEN` is configured, exactly
like `/api/mode`). All sync HTTP runs off the poll hot path in a guarded background task
(`SensorOrchestrator._survey_sync_loop`), with the DB clustering offloaded to the executor so
it never blocks the event loop, and it **fails soft** — an unreachable base node is the normal
field state, retried next cycle, never an error.

`modules/survey_sync.py` (`SurveySync`) is the mobile client; `modules/survey_store.py`
(`SurveyStore`, SQLite) backs both nodes — taskings + received findings on the fixed node,
pulled taskings + survey observations on the mobile node.

## 3. Tasking

- **Operator-initiated (primary).** A **"Task survey"** button on any surveyable WiFi contact
  in the fixed node's GUI `POST`s `/api/tasking` with the contact's portable key + evidence.
- **Auto (opt-in, `SURVEY_AUTOTASK`, default off).** The fixed node enrolls a high-severity,
  *strongly-fingerprinted* contact (`SURVEY_AUTOTASK_MIN_LEVEL`, default `high`) without an
  operator step — gated so a `mac:` contact is never tasked and the watchlist never floods.

`SurveyStore.enqueue_tasking` de-dups on identity, so re-flagging the same contact returns the
existing task rather than a duplicate.

## 4. Bed-down by AP association (the single-patrol insight)

The residence question is *"where does this device belong,"* and the strongest answer does
**not** need repeat patrols to build up dwell time. If the mobile node's wardrive hears a
**local AP beaconing the tasked device's distinctive home network** (its anchor SSID), that
AP's location *is* the bed-down — resolved in one pass. `KismetModule.poll_devices` already
populates `beaconed_ssid` for AP records (`modules/kismet.py:278`), which the matcher keys on.

`SensorOrchestrator._record_survey_hits` records two signals each poll (the node's own GPS fix
is the position):

- **AP association** (`kind='ap'`) — a local AP beaconing the tasked device's anchor SSID; its
  BSSID/SSID + location are captured.
- **Direct sighting** (`kind='device'`) — the tasked device itself, matched by the portable
  candidate keys.

`SurveyStore.compute_findings` returns a structured result: `home_ap` (the located residence),
device-sighting `clusters` (with **dwell / return / overnight kept only as confidence
annotation**, not the mechanism), and an `outcome`:

| Outcome | Meaning | WiGLE candidate? |
|---|---|---|
| **resident** | home AP found locally — here's where it lives | no |
| **seen** | device glimpsed but its home network isn't on the air locally → home elsewhere | **yes** |
| **not_located** | after `SURVEY_MIN_PATROL_POLLS` of wardriving, never found | **yes** |

## 5. The fixed node's position is the reference

The fixed node classifies *where* a device was found relative to **its own position**
(`air_geometry.resolve_reference`: a GUI-pinned `AIR_HOME_LAT/LON` wins, else the live GPS
fix). The Survey tab shows a residence's distance from the node and a locality band —
**here** (`SURVEY_IMMEDIATE_METERS`, ~where the node already sees it), **neighborhood**
(`SURVEY_NEIGHBORHOOD_METERS`, a local resident elsewhere), or **distant**. So "found in
another part of the neighborhood" reads as a local resident, while "not found locally" is the
signal that its home is elsewhere.

## 6. WiGLE escalation — a deliberate, deferred step

When a device's home AP is **not** found locally, the tasking is flagged `wigle_candidate`.
The lookup that would then locate its home from its known networks (a client's probed SSIDs, or
an AP's BSSID) is a **deliberate operator action, deferred to a follow-up PR** (see
`design-and-roadmap.md` §10). It is an **outbound, opsec-sensitive query** — it reveals what is
being investigated and from where — so it is **never automated here**. This branch surfaces the
candidate *state*; the query stays manual and off until built and opsec-gated.

## 7. Honest limits

- Only well-fingerprinted contacts are surveyable (named probe SSIDs are ~26% of WiFi clients on
  `chase`; BLE anchors sparser), so this hunts the identifiable, not everyone.
- AP association needs the home network to actually beacon within the wardrive's reach; a
  distinctive anchor keeps false matches low, but a shared SSID name is a *candidate*, not a
  certainty.
- Probing is intermittent, so a direct WiFi match lands only on polls where the device emits the
  anchor SSID — fine, the survey accumulates across many polls.

## 8. Module map

| File | Role |
|---|---|
| `modules/survey_store.py` (`SurveyStore`) | SQLite, both nodes: taskings, observations, findings; thread-safe; guarded |
| `modules/survey_sync.py` (`SurveySync`) | mobile `aiohttp` client: `pull_taskings` / `push_findings` / `reachable`; fails soft |
| `gui/server.py` | `GET/POST /api/tasking`, `POST /api/survey` (token-gated) + distance annotation |
| `modules/orchestrator.py` | `_record_survey_hits` (matcher + AP association), `_survey_sync_loop`, `_maybe_autotask` |
| `gui/static/app.js`, `gui/templates/index.html` | "Task survey" button + the Survey tab |

## 9. Next (follow-up work)

- **Operator-bounded patrols (§10)** — ✅ *shipped (#198)*: an explicit *start / end patrol* session
  replaces the blind poll-quota task lifetime, so a task never expires mid-walk.
- **The wardrive index (§11)** — *next*: the patrol also wardrives — bank every AP heard while moving
  into a local SSID → location index (honoring the ignore list), so a bed-down resolves retroactively
  and independently of what was tasked, and a patrol with no tasking still maps the area.
- The **WiGLE query client** (§6) — opsec-gated, augments-never-gates per `design-and-roadmap.md` §10.
- The **reverse §5.5 fusion** — a follower first seen on the mobile node later surfacing at the
  fixed node (the mirror of this pipeline).

## 10. Operator-bounded patrols — *shipped, PR #198*

**Why.** The Part II walk tests exposed a structural flaw independent of the anchor bug (which was
fixed in PR #196): a survey task's lifetime is a blind poll quota. `SURVEY_MIN_PATROL_POLLS`
(default 20) at the 30 s Kismet cadence closes a task after ~10 minutes, and permanently — a
completed task drops out of `open_taskings`, so the mobile node stops surveying it and it never
reopens. On walk 2 a BLE target that was physically present and scoring high was missed because its
task had already closed ~70 minutes earlier. Raising the quota only moves the arbitrary clock; the
real unit of work is *the operator's walk*, not a poll count.

**The model.** Make the patrol an explicit, operator-bounded session in the **mobile** GUI —
local-only, since the node is offline while patrolling, so it needs no round-trip to base:

- **Start patrol** snapshots the currently-open taskings and holds them open for the duration,
  regardless of poll count. Observations accumulate across the whole walk.
- **End patrol** computes findings for every held task, marks them complete, and offloads to the
  base on the next in-range sync.
- The poll quota is demoted to a **generous backstop** — an auto-end if the operator forgets to
  close a patrol, sized in hours not minutes — never the primary lifetime.

A patrol is also the natural boundary for store-and-forward: everything banked between start and end
belongs to one patrol, computed and pushed as a unit.

## 11. The wardrive index — *planned, not yet built*

**Why.** Even with operator-bounded patrols (§10), the matcher still records an observation **only**
when a device matches a *tasked* key or anchor — so anything not explicitly on the watchlist is
invisible, and walking past a device's home AP *before* it is tasked banks nothing. Collection is
coupled to tasking: a target encountered "at the wrong time," or a network you would have wanted
located but never tasked, is simply lost.

**The model — the patrol *is* the wardrive.** A patrol does two things at once, and the second does
not depend on the first:

1. **Wardrive (always, whether or not anything is tasked).** While a patrol runs, bank *every* AP the
   node hears — SSID, BSSID, and the node's GPS fix — into a local index, deduped by BSSID and keeping
   the best-signal position. Start a patrol with an empty watchlist and you still come home with a
   located map of every network in the area. The same start/end-patrol control from §10 bounds the
   collection; nothing is banked outside a patrol.
2. **Bed-down search becomes a query over that index.** Resolving a tasking is no longer "did the
   device match live at the moment I walked past it" but "does any banked AP beacon this task's anchor
   SSID?" — evaluated at end-of-patrol *and* whenever a new tasking arrives. A device tasked *after*
   the walk still resolves against what was already collected. This dissolves the §10 timing problem
   at the data layer: timing stops mattering. Direct sighting of the tasked device itself stays its
   own path (device observations); the wardrive index is the AP-association engine.

The live per-task AP matcher (`_record_survey_hits`) becomes a nice-to-have; the index is the real
engine, and a patrol with zero tasks is still a useful **standalone environmental survey** — the
opsec-local substitute for the deferred outbound WiGLE lookup (§6): the same SSID → location answer,
sourced from the node's own passive collection instead of a third-party query.

**Decisions (locked).**

- **Honor the ignore list.** The wardrive respects the existing MAC / OUI / SSID ignore list, so the
  operator's own home network and anything muted are never banked. This is the privacy/opsec answer
  to "exclude my own street," and it reuses the filter already in the capture path — no new exclusion
  mechanism.
- **APs only (v1).** Bed-down-by-AP needs only APs, and it keeps the index small. Banking client
  devices (for retroactive *direct* sighting) is a later extension, not v1.
- **Retention ~90 days, size-bounded by dedup.** The index grows with *area covered*, not time —
  dedup by BSSID means re-walking the same street never inflates it. A retention sweep
  (`SURVEY_WARDRIVE_RETENTION_DAYS`, default 90) bounds it like the entity store. Node-local,
  gitignored, never committed.

**Data model.** A new `survey_wardrive` table in `SurveyStore` (BSSID primary key; SSID; best
lat/lon/RSSI; first/last seen; `patrol_id`; observation count). Banking is a step in the mobile
survey poll path, gated on an active patrol **and** the ignore filter; bed-down resolution is a lookup
of a tasked anchor SSID against the index. As the survey subsystem is lifted out of the orchestrator
into `modules/survey_coordinator.py` (`SurveyCoordinator`, held by the orchestrator as `.survey`),
the banking step and the index query live there.

**Boundaries.**

- **Opsec.** The index maps real, third-party networks to coordinates — captured data. Node-local,
  gitignored, never committed; the same rule that governs `data/` and the ignore lists.
- **No duplication of Kismet.** Kismet writes the WiGLE CSV at session end for the *upload* — a
  write-once export. The wardrive index is a different artifact: a live, queryable match store the
  survey logic reads during and after a patrol. We build the index, not re-collect what Kismet has.

**Sequencing.** #196 (anchor) and §10 operator-bounded patrols (#198) are **shipped**; §11 (the
wardrive index) is next and rides on the patrol session as its collection window.

---

# Part II — Validation: the recon-pair test plan

The bench is green (39 survey unit tests + a real-HTTP end-to-end integration test). This is the
**on-node** proof, phased, using the roadmap's soak-plan discipline (knobs → procedure → pass bar
→ safety). It runs on the live pair.

**Nodes.** `chase` = fixed base station (Pi 4B+, GUI on **:8088**, `GUI_TOKEN` set),
`survkis` = mobile spoke (Pi 3B+, WiFi+GPS, `NODE_MODE=mobile`, carried).

## Prerequisites & config

Deploy the `feat/recon-node-survey` branch to **both** nodes (running unmerged validation code on
the test nodes is the accepted soak method), install, restart. **Deploy once — never hot-edit the
loop on a live node** (a blocking edit trips the systemd watchdog). **Confirm `NODE_MODE` before
every restart** (wrong mode crash-loops the service).

| Node | Key `.env` | Value |
|---|---|---|
| **chase** (fixed) | `SURVEY_ENABLED` | `true` |
| | `GUI_TOKEN` / `GUI_PORT` | already set / `8088` (readsb owns 8080) |
| | `AIR_HOME_LAT` / `AIR_HOME_LON` | *optional* pin for stable distance banding (else live GPS) |
| | `SURVEY_AUTOTASK` | `off` (default; the button is primary) |
| **survkis** (mobile) | `SURVEY_ENABLED` | `true` |
| | `SURVEY_FIXED_URL` | `http://chasingyourtail.local:8088` (or chase's IP) |
| | `SURVEY_TOKEN` | chase's `GUI_TOKEN` value |
| | `NODE_MODE` | `mobile` (already) |
| | `KISMET_ACTIVE_WINDOW_SECONDS` | `90`–`120` (mobile-scoring hygiene) |
| | `SURVEY_MIN_PATROL_POLLS` | `20` default; lower for a fast not-found test |

## Ph0 — Plumbing on the real pair (no field movement)

Enable both, restart, task a chase WiFi contact via the **"Task survey"** button (or
`POST /api/tasking`). **Pass:** survkis's sync loop reports the fixed node reachable and pulls the
tasking (it appears in survkis's `survey.db` / logs); a wrong `SURVEY_TOKEN` is refused (401/403);
with chase unreachable, survkis keeps running (fail-soft, no errors). Confirms auth + pull/push
over the LAN before any driving.

## Ph1 — AP-association bed-down walk-test (the headline proof)

Task a known device whose **distinctive home SSID is beaconed by a local AP** (e.g. your phone +
its home-router SSID, or a cooperating AP). Carry survkis on a loop that passes within WiFi range
of that AP. **Pass:** survkis records a `kind='ap'` observation, `compute_findings` → **`resident`**
with `home_ap` = that AP (BSSID/SSID/location), offloads on return, and chase's **Survey tab** shows
the home AP + its distance/locality from chase — resolved in a **single patrol**, no dwell needed.

## Ph2 — Not-found → WiGLE-candidate flag

Task a device whose home AP is **not** in the surveyed area (or an absent device). Survey past
`SURVEY_MIN_PATROL_POLLS`. **Pass:** outcome **`not_located`** (or `seen` if the device itself was
glimpsed), `wigle_candidate` flagged in the Survey tab, and **no WiGLE query fires** (deferred +
manual). Confirms the escalation *state* surfaces and stays inert.

## Ph3 — Store-and-forward under real disconnect + classification

Take survkis out of chase's LAN range, run a survey **offline** (observations bank locally), return
to base. **Pass:** on reconnect survkis pulls new taskings and pushes the banked findings with **no
data loss** and no errors while disconnected. Also validate the distance banding (here /
neighborhood / distant vs chase's reference) and, optionally, the auto-task path
(`SURVEY_AUTOTASK=on` enrolls a high-severity strong-fp contact).

## Ph4 — Multi-day pair endurance

Run the pair for a multi-day window. **Pass:** survkis loop stable (no watchdog SIGABRT from the
sync loop — it is off the poll hot path), `survey.db` bounded (observation retention sweep,
`SURVEY_OBS_RETENTION_DAYS`), chase endpoints stable, no memory drift on either node, sync survives
a reboot of either. **Judge liveness by counter advancement, not health-banner ✓ flags** (the node
has silently stalled green before).

## Safety rails

Confirm `NODE_MODE` before each restart; keep a `survey.db` backup; survey is off-by-default so a
bad value only disables the feature; read counters, not ✓ flags; deploy the branch, never hot-patch
a live node.
