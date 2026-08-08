# Recon-Pair Survey (SurveyCoordinator, SurveyStore, SurveySync)

The survey subsystem turns two nodes into a **hunting team**: a **fixed base** that decides *which* contact is worth investigating, and a **mobile patrol** that goes and finds *where that device beds down*. It is entirely optional (`SURVEY_ENABLED=false` by default), guarded so a survey-store or network failure can never touch capture or detection, and completely inert when off. Full design: `docs/design-recon-pair.md` (§5.5 / §10 / §11).

## Overview

| Component | File | Role |
|---|---|---|
| **SurveyCoordinator** | `modules/survey_coordinator.py` | The survey engine, lifted out of the orchestrator; owns the store + sync client |
| **SurveyStore** | `modules/survey_store.py` | Durable SQLite (`data/survey.db`) — taskings, patrols, observations, findings, wardrive index; thread-safe (the GUI reads it) |
| **SurveySync** | `modules/survey_sync.py` | Mobile-node `aiohttp` client to the fixed node's survey endpoints; store-and-forward, fails soft |

## Design rationale

A fixed node answers "who is watching me" but cannot move to find *where* a suspicious device lives. A mobile node can walk a neighbourhood but has no learned baseline to know what is suspicious. Pairing them plays to both strengths: the base flags a strongly-fingerprinted contact and tasks the patrol; the patrol locates the device (or its home AP) by association as it moves, and reports the bed-down back. Neither node's core job — baseline scoring on the base, capture on the patrol — is disturbed, because the survey logic lives in its own coordinator with three narrow seams into the orchestrator and holds no reference back to it.

## The three seams into the orchestrator

The orchestrator holds the coordinator as `self.survey` and touches it in exactly three places:

1. **`record_hits(devices, fix)`** — every Kismet poll, the **mobile** matcher checks the live device list against open taskings and records where each was seen (with the current GPS fix).
2. **`note_flagged_contact(event, device, contact, contact_key)`** — for every flagged Wi-Fi contact, returns the tasking *evidence* (or `None` for a non-portable `mac:` contact). This drives the GUI "Task survey" button and, on a **fixed** node with `SURVEY_AUTOTASK` on, the opt-in auto-task.
3. **`sync_loop()`** — started by `main` as a background task on a syncing mobile node (`sync_configured`); pulls taskings from the base and pushes findings back.

## Workflow (fixed base + mobile patrol)

```
FIXED (base node)                       MOBILE (patrol node)
  flag a strong contact
  → note_flagged_contact → tasking       ← pull_taskings (sync_loop)
                                          start_patrol
                                          record_hits every poll → observations
                                          wardrive index banks every AP heard
                                          compute_findings → bed-down location
  ingest_result ←───────────────────────  push_findings
  findings shown in the GUI
```

- **Taskings** carry the target's rotation-stable identity key and anchor SSID — you can only task a device you can re-identify across MAC rotation.
- **Patrols** bound the effort: a task patrolled `SURVEY_MIN_PATROL_POLLS` without a hit, or a patrol left running past `SURVEY_PATROL_MAX_HOURS`, is closed out so nothing sits open forever.
- **Wardrive index** (design §11): while a patrol runs, every AP heard is banked (deduped by BSSID). A bed-down can then be resolved *retroactively* by querying the index for a task's anchor SSID — even for a device tasked after the walk.
- **Findings** cluster observations into an immediate / neighbourhood bed-down using the configured distances plus a night-hours residence heuristic.

## Key methods

- **SurveyStore** — `enqueue_tasking` / `open_taskings` / `set_status`; `start_patrol` / `end_patrol` / `patrol_status`; `record_survey_observation`; `compute_findings`; `ingest_result` / `findings_for`; `upsert_wardrive_ap` / `wardrive_aps_for_ssid`; `prune_observations` / `prune_wardrive`; `close`.
- **SurveySync** — `configured`, `reachable`, `pull_taskings`, `push_findings`.
- **SurveyCoordinator** — `record_hits`, `note_flagged_contact`, `sync_loop`, `sync_configured`.

## GUI endpoints (GUI_TOKEN-gated)

- `POST /api/tasking` — create a survey tasking (the operator "Task survey" button); `GET` lists them.
- `GET/POST /api/survey` — observations and computed findings for a task.
- `GET/POST /api/patrol` — start / end / status of the mobile patrol.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SURVEY_ENABLED` | false | Master switch; the store is not even opened when off |
| `SURVEY_NODE_ID` | hostname | Stamped on findings so the base can attribute a survey |
| `SURVEY_FIXED_URL` | (empty) | Mobile → base URL; enables the sync loop |
| `SURVEY_TOKEN` | (empty) | Auth for the sync endpoints |
| `SURVEY_SYNC_INTERVAL_SECONDS` | (see source) | Store-and-forward cadence |
| `SURVEY_AUTOTASK` | off | Fixed-node opt-in auto-task (the operator button is the primary path) |
| `SURVEY_AUTOTASK_MIN_LEVEL` | high | Severity gate for auto-task |
| `SURVEY_MIN_PATROL_POLLS` | 20 | Patrol effort before a task is deemed "not encountered" |
| `SURVEY_PATROL_MAX_HOURS` | 12 | Backstop auto-end for a forgotten patrol |
| `SURVEY_WARDRIVE_RETENTION_DAYS` | 90 | Wardrive index retention |
| `SURVEY_OBS_RETENTION_DAYS` | (see source) | Survey observation retention |
| `SURVEY_*_METERS` / `SURVEY_VISIT_GAP_SECONDS` / `SURVEY_NIGHT_HOURS` | (see source) | Bed-down clustering distances, distinct-visit gap, night-residence heuristic |

## Pitfalls

- **Off by default and inert.** With `SURVEY_ENABLED=false` the store is never opened and every coordinator method no-ops; nothing here runs on an ordinary node.
- **Only strong contacts are surveyable.** A non-portable `mac:` contact yields no tasking evidence — you cannot task a device you cannot re-identify across rotation.
- **Store-and-forward, fail-soft.** The mobile node keeps patrolling and banking when the base is unreachable; findings sync when it returns. A network failure never blocks the walk.
- **Wardrive needs a GPS fix.** Banking silently drops APs when the node has no position; the coordinator logs one edge-triggered warning so an operator mid-walk knows.
- **Guarded on both nodes.** A survey-store or sync failure is caught and never touches capture or detection.

## Related modules

- [SensorOrchestrator](orchestrator.md) — holds `self.survey`; the three seams (`record_hits`, `note_flagged_contact`, `sync_loop`).
- [Identity](identity.md) — supplies the rotation-stable key and anchor SSID a tasking is built on.
- [Durable Stores](stores.md) — `SurveyStore` is a third SQLite store, separate from EntityStore / BaselineStore.
- [Outputs](outputs.md) — the survey GUI endpoints.

## Hardware notes

| Node | Survey role |
|---|---|
| **Fixed node (base)** | Flags contacts, serves taskings, ingests findings; GUI on 8088 (survey endpoints need `?token=`) |
| **Mobile node (patrol)** | Pulls taskings, walks, records observations + wardrive, pushes findings back |

## See also

- `docs/design-recon-pair.md` — full design (§5.5 tasking, §10 patrol bounds, §11 wardrive index) and the on-node validation plan.
