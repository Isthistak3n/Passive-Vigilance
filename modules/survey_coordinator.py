'''survey_coordinator — the recon-pair survey logic (design §5.5 / §10), lifted out
of :class:`SensorOrchestrator` so the poll loop is not also the survey engine.

The orchestrator keeps three one-line seams into this coordinator:

* per Kismet poll it hands over the device list so the **mobile** matcher can record
  where any open tasking was located (:meth:`record_hits`);
* for every flagged WiFi contact it asks for the tasking evidence, which on a
  **fixed** node also drives the opt-in auto-task (:meth:`note_flagged_contact`);
* ``main`` starts the store-and-forward :meth:`sync_loop` as a background task when
  this node is a syncing mobile node (:attr:`sync_configured`).

Everything here is **guarded** — a survey-store or network failure must never touch
capture or detection — and inert when ``survey_store`` is None (feature off). The
coordinator holds no reference back to the orchestrator: it is handed the survey
store, the resolved node mode, and the shutdown event, and is given the live GPS fix
at the one call site that needs it.
'''
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Optional

from modules import device_identity
from modules.survey_sync import SurveySync
from modules.wifi_fingerprint import anchored_identity_key

logger = logging.getLogger(__name__)

# Alert-level ordering for the survey auto-task severity gate (weakest -> strongest).
_SEVERITY_ORDER = {"suspicious": 0, "likely": 1, "high": 2}


class SurveyCoordinator:
    '''Owns the recon-pair survey matcher, fixed-node tasking, and the mobile-node
    store-and-forward sync. See the module docstring for the seams into the
    orchestrator.

    Args:
        survey_store: the durable :class:`~modules.survey_store.SurveyStore`, or None
            when the feature is off (every method then no-ops).
        node_mode: the resolved ``"fixed"``/``"mobile"`` mode; gates the mobile-only
            matcher and the fixed-only auto-task.
        stop_event: the orchestrator's shutdown event, watched by the sync loop.
    '''

    def __init__(self, survey_store, node_mode: str,
                 stop_event: asyncio.Event) -> None:
        self.survey_store = survey_store
        self._node_mode = node_mode
        self._stop_event = stop_event
        # Fixed-node auto-tasking (opt-in): enroll a high-severity, strongly-
        # fingerprinted contact as a survey target without an operator step. Off by
        # default so the operator "Task survey" button is the primary path.
        self._survey_autotask = os.getenv("SURVEY_AUTOTASK", "off").strip().lower() in (
            "on", "true", "1", "yes")
        self._survey_autotask_min_level = os.getenv(
            "SURVEY_AUTOTASK_MIN_LEVEL", "high").strip().lower()
        # This node's identifier, stamped on findings/taskings so the fixed node can
        # attribute a survey to the node that ran it.
        self._survey_node = (os.getenv("SURVEY_NODE_ID", "").strip()
                             or socket.gethostname())
        # Taskings we've already flipped to 'surveying' this session (avoid a status
        # write every poll a target is seen).
        self._survey_started: set = set()
        # Patrol effort per task (polls processed since it was pulled) — a task the
        # mobile node has patrolled this many polls without locating the device OR its
        # home AP is reported "not located" (a WiGLE candidate), so a device that was
        # simply never encountered doesn't sit silently forever.
        self._survey_patrol_polls: dict = {}
        self._survey_min_patrol_polls = int(
            os.getenv("SURVEY_MIN_PATROL_POLLS", "20"))
        # Operator-bounded patrol backstop (design §10): a patrol left running past this
        # many hours is auto-ended, so a forgotten "end patrol" can't hold tasks open
        # forever. Generous — hours, not the ~10-min poll quota it supersedes.
        self._patrol_max_seconds = float(
            os.getenv("SURVEY_PATROL_MAX_HOURS", "12")) * 3600.0
        # Wardrive index (design §11): while a patrol runs, bank every AP heard so a
        # bed-down can be resolved by querying the index for a task's anchor SSID —
        # retroactively, and even for a device tasked after the walk. Retention bounds
        # the index (dedup by BSSID already caps it by area). _wardrive_recorded avoids
        # re-recording the same (task, AP) as an observation each cycle.
        self._wardrive_retention_days = int(
            os.getenv("SURVEY_WARDRIVE_RETENTION_DAYS", "90"))
        self._wardrive_recorded: set = set()
        self._last_wardrive_prune = 0.0
        # Mobile-node client to the fixed node's survey endpoints. Inert (no URL)
        # unless SURVEY_FIXED_URL is set; the sync loop no-ops when not configured.
        self._survey_sync = SurveySync(
            os.getenv("SURVEY_FIXED_URL", ""), os.getenv("SURVEY_TOKEN", ""))

    @property
    def sync_configured(self) -> bool:
        '''True when this node is a mobile node with a fixed-node URL set — the only
        case in which the sync loop does anything.'''
        return self.survey_store is not None and self._survey_sync.configured

    # ------------------------------------------------------------------
    # Portable identity (design §5.5)
    # ------------------------------------------------------------------

    @staticmethod
    def device_candidate_keys(device: dict) -> set:
        """The portable identity keys an observed device *could* be tasked under.

        A survey tasking names a device by its rotation-stable content key. BLE keys
        (``ble-fp:``) are pure advertisement content, so a device has exactly one and
        it is directly portable across nodes. A WiFi ``wifi-fp:`` key is the IE-set
        hash anchored on ONE distinctive probed SSID — but which SSID the FIXED node
        anchored on depends on its own local SSID rarity, so we cannot assume the
        mobile node would pick the same anchor. Instead we enumerate the key this
        device would produce for EACH of its own named probed SSIDs as the anchor:
        the tasked key is in that set whenever this device probes the SSID the fixed
        node anchored on. This makes the match deterministic without either node
        having to agree on anchor selection. (Probing is intermittent, so a match may
        land only on the polls where the device emits the anchor SSID — fine, the
        survey accumulates observations across many polls.)
        """
        if device_identity.is_ble_device(device):
            k = device_identity.strong_fingerprint(device)
            return {k} if k else set()
        probe_fp = device.get("probe_fingerprint")
        if not probe_fp:
            return set()
        ssids = {s for s in (device.get("probe_ssids") or [])
                 if isinstance(s, str) and s.strip()}
        return {anchored_identity_key(probe_fp, s) for s in ssids}

    # ------------------------------------------------------------------
    # Mobile matcher (per Kismet poll)
    # ------------------------------------------------------------------

    def record_hits(self, devices: list, fix: Optional[dict]) -> None:
        """Mobile side: for every open tasking, record where it was located this poll.

        Two location signals, in order of strength:
          * **AP association** — a local AP beaconing the tasked device's distinctive
            home network (its anchor SSID) is heard; its BSSID/SSID + the node's GPS
            fix are the residence. This resolves a bed-down in a SINGLE patrol, no dwell
            accumulation needed (the operator's insight).
          * **Direct sighting** — the tasked device itself is observed (matched by the
            portable candidate keys); where the node is standing is recorded.

        Also advances each open task's patrol effort so a device that is never
        encountered eventually reads "not located" (a WiGLE candidate) rather than
        staying silent. *fix* is the node's own current GPS fix; fully guarded."""
        if self.survey_store is None or self._node_mode != "mobile":
            return
        now = datetime.now(timezone.utc)
        fix = fix or {}
        lat, lon = fix.get("lat"), fix.get("lon")
        # Wardrive (design §11): while a patrol runs, bank every AP heard into the local
        # index, independent of what's tasked — so the area is mapped even with an empty
        # watchlist and a bed-down resolves retroactively. Honors the ignore list (whose
        # devices are already filtered out of `devices` upstream in poll_devices).
        self._bank_wardrive(devices, lat, lon, now)
        try:
            taskings = self.survey_store.open_taskings()
        except Exception as exc:
            logger.debug("survey open_taskings failed: %s", exc)
            return
        if not taskings:
            return
        key_to_task = {t["identity_key"]: t["task_id"] for t in taskings}
        tasked_keys = set(key_to_task)
        # Distinctive home SSID (the anchor) -> task ids, for AP-association matching.
        ssid_to_tasks: dict = {}
        for t in taskings:
            anchor = self._task_anchor(t)
            if anchor:
                ssid_to_tasks.setdefault(anchor.lower(), []).append(t["task_id"])

        # Patrol effort: one poll processed for every open task this cycle.
        for t in taskings:
            tid = t["task_id"]
            self._survey_patrol_polls[tid] = self._survey_patrol_polls.get(tid, 0) + 1

        for d in devices:
            # Direct device sighting.
            for key in (self.device_candidate_keys(d) & tasked_keys):
                self._record_obs(key_to_task[key], now, lat, lon,
                                 d.get("last_signal"))
            # AP association: is this a local AP beaconing a tasked device's home SSID?
            beaconed = (d.get("beaconed_ssid") or "").strip().lower()
            if beaconed and beaconed in ssid_to_tasks:
                for tid in ssid_to_tasks[beaconed]:
                    self._record_obs(
                        tid, now, lat, lon, d.get("last_signal"),
                        kind="ap", bssid=d.get("macaddr"),
                        ssid=d.get("beaconed_ssid"))

    def _record_obs(self, tid, now, lat, lon, rssi, *,
                    kind="device", bssid=None, ssid=None) -> None:
        """Write one survey observation and flip the task to 'surveying' on first hit."""
        try:
            self.survey_store.record_survey_observation(
                tid, timestamp=now, lat=lat, lon=lon, rssi=rssi,
                kind=kind, bssid=bssid, ssid=ssid)
            if tid not in self._survey_started:
                self.survey_store.set_status(tid, "surveying")
                self._survey_started.add(tid)
        except Exception as exc:
            logger.debug("survey observation write failed: %s", exc)

    # ------------------------------------------------------------------
    # Wardrive index (design §11)
    # ------------------------------------------------------------------

    @staticmethod
    def _task_anchor(tasking: dict) -> str:
        """The tasked device's home SSID (the AP-association key). Falls back to the
        label — which IS the anchor for a strong WiFi contact — when a task carries no
        explicit anchor, and never applies to BLE (no beaconed home network)."""
        ev = tasking.get("evidence") or {}
        anchor = (ev.get("anchor") or "").strip()
        if not anchor and ev.get("modality") != "ble":
            anchor = (ev.get("label") or "").strip()
        return anchor

    def _bank_wardrive(self, devices: list, lat, lon, now) -> None:
        """Bank every AP heard this poll into the wardrive index — but only while a
        patrol runs (nothing is collected outside a patrol) and only with a GPS fix (an
        AP with no position can't be located). APs only in v1: a record with a non-empty
        ``beaconed_ssid`` is an AP beaconing that network."""
        if lat is None or lon is None:
            return
        try:
            patrol = self.survey_store.active_patrol()
        except Exception as exc:
            logger.debug("wardrive patrol check failed: %s", exc)
            return
        if patrol is None:
            return
        pid = patrol.get("patrol_id")
        for d in devices:
            ssid = (d.get("beaconed_ssid") or "").strip()
            bssid = d.get("macaddr")
            if not ssid or not bssid:
                continue
            rssi = d.get("last_signal")
            rssi = None if rssi in (0, None) else rssi  # 0 = placeholder, not a reading
            try:
                self.survey_store.upsert_wardrive_ap(
                    bssid=bssid, ssid=ssid, lat=lat, lon=lon, rssi=rssi,
                    timestamp=now, patrol_id=pid)
            except Exception as exc:
                logger.debug("wardrive bank failed: %s", exc)

    def _resolve_from_wardrive(self, taskings: list) -> None:
        """Retroactive bed-down: for each open task, fold any banked AP that beacons its
        anchor SSID into that task's observations — so a device tasked *after* the walk,
        or whose home AP was banked on a poll the live matcher didn't catch, still
        resolves. Deduped per (task, BSSID) so a cycle never re-records the same AP."""
        now = datetime.now(timezone.utc)
        for t in taskings:
            anchor = self._task_anchor(t)
            if not anchor:
                continue
            tid = t["task_id"]
            try:
                aps = self.survey_store.wardrive_aps_for_ssid(anchor)
            except Exception as exc:
                logger.debug("wardrive lookup failed: %s", exc)
                continue
            for ap in aps:
                bssid = ap.get("bssid")
                if not bssid or (tid, bssid) in self._wardrive_recorded:
                    continue
                self._record_obs(tid, now, ap.get("lat"), ap.get("lon"),
                                 ap.get("rssi"), kind="ap", bssid=bssid,
                                 ssid=ap.get("ssid"))
                self._wardrive_recorded.add((tid, bssid))

    # ------------------------------------------------------------------
    # Fixed-node tasking (per flagged contact)
    # ------------------------------------------------------------------

    def note_flagged_contact(self, event, device, contact,
                             contact_key) -> Optional[dict]:
        """Return the tasking evidence for a flagged WiFi contact (None when it has no
        portable key), and — on a fixed node with auto-task enabled — enqueue it as a
        survey target. This is the orchestrator's single survey seam on the flagged
        path: the returned evidence drives the GUI "Task survey" button, and the
        auto-task side effect enrolls genuine threats without an operator step."""
        evidence = self._build_evidence(event, device, contact, contact_key)
        self._maybe_autotask(event, contact, contact_key, evidence)
        return evidence

    def _build_evidence(self, event, device, contact, contact_key) -> Optional[dict]:
        """Assemble the tasking evidence for a surveyable contact, or None when the
        contact has no portable key (a bare ``mac:`` device cannot be re-identified on
        another node). Carried on the WiFi event dict so the operator's "Task survey"
        button can echo it back, and reused by the fixed-node auto-task path."""
        # No survey store -> the feature is off; skip the work (and the per-event field).
        if self.survey_store is None:
            return None
        if not contact_key or str(contact_key).startswith("mac:"):
            return None
        modality = "ble" if (device and device_identity.is_ble_device(device)) else "wifi"
        # The AP-association bed-down matches on this anchor (the device's distinctive
        # home SSID). The live device carries ``fp_anchor`` ONLY on a poll where it
        # freshly probes that SSID, and probing is intermittent — so a task issued on
        # any ordinary poll used to capture ``None`` and could never resolve a residence.
        # The rotation-stable fingerprint label IS that anchor SSID for a strong WiFi
        # contact, so source the anchor from it (falling back through the live fields).
        # BLE has no beaconed home network, so its anchor stays None (matched by its
        # content-portable key via direct sighting).
        anchor = None
        if modality == "wifi":
            anchor = ((device or {}).get("fp_anchor")
                      or (device or {}).get("fp_anchor_medium")
                      or event.fingerprint_label or None)
        return {
            "modality": modality,
            "identity_key": contact_key,
            "designator": contact,
            "manufacturer": event.manufacturer,
            "device_type": event.device_type,
            # WiFi match/debug aids (None for BLE, whose key is content-portable).
            "probe_fingerprint": (device or {}).get("probe_fingerprint"),
            "anchor": anchor,
            "label": event.fingerprint_label,
        }

    def _maybe_autotask(self, event, contact, contact_key, evidence) -> None:
        """Fixed-node opt-in: enroll a high-severity, strongly-fingerprinted contact
        as a survey target. De-dup + surveyability are enforced by the store and by
        ``evidence`` being None for a ``mac:`` contact; the severity gate keeps the
        watchlist to genuine threats. Guarded."""
        if (not self._survey_autotask or self._node_mode != "fixed"
                or self.survey_store is None or evidence is None):
            return
        bar = _SEVERITY_ORDER.get(self._survey_autotask_min_level, 2)
        if _SEVERITY_ORDER.get(event.alert_level, -1) < bar:
            return
        try:
            self.survey_store.enqueue_tasking(
                contact_key, designator=contact,
                reason=self._autotask_reason(event), evidence=evidence,
                origin_node=self._survey_node)
        except Exception as exc:
            logger.debug("survey auto-task failed: %s", exc)

    @staticmethod
    def _autotask_reason(event) -> str:
        """Best-effort human reason from the score breakdown (novelty/off-schedule/
        approaching), else the alert level."""
        bd = getattr(event, "score_breakdown", None) or {}
        for sig in ("novelty", "approaching", "off_schedule", "returning"):
            if bd.get(sig):
                return sig
        return f"auto:{event.alert_level}"

    # ------------------------------------------------------------------
    # Store-and-forward sync (mobile background task)
    # ------------------------------------------------------------------

    async def sync_loop(self) -> None:
        """Mobile side: periodically, when the fixed node is reachable, pull open
        taskings and push computed findings for any surveyed target — the
        store-and-forward exchange (design §5.5). A separate guarded background task
        (like the adaptation sweep); all DB work is offloaded to the executor so the
        clustering never blocks the event loop, and all HTTP fails soft (an
        unreachable base node is the normal field state)."""
        if not self.sync_configured:
            return  # not a syncing mobile node — nothing to do
        interval = float(os.getenv("SURVEY_SYNC_INTERVAL_SECONDS", "120"))
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if self._stop_event.is_set():
                break
            # Retention sweep first — independent of reachability, throttled hourly — so
            # the wardrive index and observation history stay bounded on a node that is
            # offline for long stretches (design §11).
            await loop.run_in_executor(None, self._maybe_prune)
            try:
                if not await self._survey_sync.reachable():
                    continue
                pulled = await self._survey_sync.pull_taskings()
                if pulled:
                    await loop.run_in_executor(None, self._ingest_pulled_taskings, pulled)
                # Compute survey results (DB-heavy) off the loop, then push each.
                prepared = await loop.run_in_executor(None, self._prepare_findings)
                for tid, result in prepared:
                    ok = await self._survey_sync.push_findings(
                        tid, result, self._survey_node)
                    if ok:
                        await loop.run_in_executor(
                            None, self.survey_store.set_status, tid, "complete")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Survey sync error (capture unaffected): %s", exc)

    def _ingest_pulled_taskings(self, pulled: list) -> None:
        for t in pulled:
            try:
                self.survey_store.upsert_tasking(t)
            except Exception as exc:
                logger.debug("survey tasking upsert failed: %s", exc)

    def _maybe_prune(self) -> None:
        """Throttled (hourly) retention sweep of the wardrive index and the survey
        observation history, so both stay bounded on a long-offline node. Guarded — a
        prune failure never disturbs the sync."""
        if time.monotonic() - self._last_wardrive_prune < 3600.0:
            return
        self._last_wardrive_prune = time.monotonic()
        try:
            self.survey_store.prune_wardrive(self._wardrive_retention_days)
            self.survey_store.prune_observations()
        except Exception as exc:
            logger.debug("survey retention sweep failed: %s", exc)

    def _prepare_findings(self) -> list:
        """Compute ``(task_id, result)`` for every open task the mobile node has either
        located OR patrolled long enough to declare "not located" — executor-thread work
        so the clustering never blocks the loop. A task pulled but not yet patrolled the
        minimum is skipped (still surveying), so we never push a premature not-found."""
        out = []
        try:
            taskings = self.survey_store.open_taskings()
        except Exception as exc:
            logger.debug("survey open_taskings failed: %s", exc)
            return out
        # Retroactive bed-down (design §11): fold any banked wardrive AP that beacons a
        # task's anchor into its observations before we compute — so timing (tasked
        # before/after the encounter) no longer decides whether a residence resolves.
        self._resolve_from_wardrive(taskings)
        # Patrol state (design §10). An ACTIVE patrol suspends the poll-quota closure —
        # the walk is the unit of work, so nothing closes mid-patrol and a task never
        # expires before the operator finds it. Ending the patrol (pending finalize)
        # closes every still-open task as a batch. A runaway patrol is auto-ended by a
        # generous backstop. With no patrol at all, the legacy quota behavior stands.
        patrol = pending = None
        try:
            patrol = self.survey_store.active_patrol()
            if patrol is not None and self._patrol_backstop_exceeded(patrol):
                self.survey_store.end_patrol()
                patrol = None
            if patrol is None:
                pending = self.survey_store.patrol_pending_finalize()
        except Exception as exc:
            logger.debug("survey patrol state read failed: %s", exc)
        if patrol is not None:
            return out  # patrol active — defer all closure to end-of-patrol
        for t in taskings:
            tid = t["task_id"]
            try:
                if pending is None:
                    # Legacy (no patrol): close once located or patrolled out.
                    has_obs = self.survey_store.observation_count(tid) > 0
                    patrolled = (self._survey_patrol_polls.get(tid, 0)
                                 >= self._survey_min_patrol_polls)
                    if not (has_obs or patrolled):
                        continue  # still surveying — don't report yet
                # Finalize mode (patrol just ended) closes every open task as a unit.
                result = self.survey_store.compute_findings(
                    tid, survey_node=self._survey_node)
                out.append((tid, result))
            except Exception as exc:
                logger.debug("survey compute_findings failed for %s: %s", tid, exc)
        if pending is not None:
            try:
                self.survey_store.mark_patrol_finalized(pending["patrol_id"])
            except Exception as exc:
                logger.debug("mark_patrol_finalized failed: %s", exc)
        return out

    def _patrol_backstop_exceeded(self, patrol: dict) -> bool:
        """A patrol running longer than SURVEY_PATROL_MAX_HOURS is auto-ended so a
        forgotten "end patrol" can't hold tasks open indefinitely."""
        try:
            started = datetime.fromisoformat(patrol.get("started_at"))
        except (TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - started).total_seconds() >= self._patrol_max_seconds
