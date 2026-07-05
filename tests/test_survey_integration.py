"""End-to-end recon-pair test — the whole loop over REAL HTTP.

Stands up a live FIXED-node GUIServer (Flask, in its daemon thread) with a
SurveyStore, then drives the MOBILE side against it exactly as the orchestrator +
sync loop would: pull the tasking over the wire, match observed devices with the
real portable-key matcher, cluster the sightings into a bed-down finding, and push
it back — asserting the finding lands on the fixed node and completes the tasking.

This exercises the seams the unit tests mock out: the real endpoints, the real
`aiohttp` client, cross-node key portability, and the dwell/return clustering.
"""

import asyncio
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from modules.orchestrator import SensorOrchestrator
from modules.survey_store import SurveyStore
from modules.survey_sync import SurveySync
from modules.wifi_fingerprint import anchored_identity_key

TOKEN = "integration-tok"
T0 = datetime(2026, 5, 1, 20, 0, 0, tzinfo=timezone.utc)
SPOT = (37.7749, -122.4194)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_reachable(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def test_fixed_to_mobile_survey_roundtrip():
    fixed_store = SurveyStore(":memory:")
    port = _free_port()
    with patch.dict(os.environ, {"GUI_TOKEN": TOKEN}):
        from gui.server import GUIServer
        gui = GUIServer(host="127.0.0.1", port=port, survey_store=fixed_store)
    if gui.app is None:
        pytest.skip("Flask not installed")
    assert gui.start() is True
    assert _wait_reachable(port), "fixed-node GUI did not come up"

    base = f"http://127.0.0.1:{port}"

    # The fixed node flags a strongly-fingerprinted contact and tasks it. The key is
    # the IE-hash anchored on a distinctive probed SSID (what the fixed node would
    # produce from its own anchor selection).
    probe_fp = 918273
    anchor = "HOME_NET_5G"
    identity_key = anchored_identity_key(probe_fp, anchor)
    tid = fixed_store.enqueue_tasking(
        identity_key, designator="CLI-HOME_NET_5G-1", reason="novelty",
        evidence={"modality": "wifi", "identity_key": identity_key,
                  "probe_fingerprint": probe_fp, "anchor": anchor},
        now=T0)

    # A device the MOBILE node observes — same IE hash, probes the anchor SSID among
    # others. The mobile node need NOT have chosen the same anchor: the matcher tries
    # each probed SSID, so the tasked key is recovered.
    observed = {
        "macaddr": "aa:bb:cc:dd:ee:ff",
        "type": "Wi-Fi Client",
        "probe_fingerprint": probe_fp,
        "probe_ssids": ["xfinitywifi", anchor, "attwifi"],
        "last_signal": -47,
    }

    mobile_store = SurveyStore(":memory:")
    sync = SurveySync(base, TOKEN)

    async def drive_mobile():
        # 1. Pull the tasking over the wire.
        assert await sync.reachable()
        pulled = await sync.pull_taskings()
        assert pulled and pulled[0]["task_id"] == tid
        for t in pulled:
            mobile_store.upsert_tasking(t)

        # 2. The matcher: the observed device's candidate keys must include the
        #    tasked key (cross-node portability), so it is recorded as a hit.
        candidates = SensorOrchestrator._device_candidate_keys(observed)
        assert identity_key in candidates, "portable-key match failed across nodes"

        # 3. Simulate a bed-down: many sightings 60 s apart at one spot (10 min dwell).
        tasked = mobile_store.open_identity_keys()
        assert identity_key in tasked
        for i in range(11):
            mobile_store.record_survey_observation(
                tasked[identity_key], timestamp=T0 + timedelta(seconds=60 * i),
                lat=SPOT[0], lon=SPOT[1], rssi=observed["last_signal"])

        # 4. Compute the survey result and push it back over the wire.
        result = mobile_store.compute_findings(
            tid, tz=timezone.utc, survey_node="mobile-test")
        assert result["clusters"] and result["clusters"][0]["dwell_seconds"] >= 600
        assert await sync.push_findings(tid, result, "mobile-test") is True

    asyncio.run(drive_mobile())

    # 5. The fixed node now holds the survey result and the tasking is complete.
    got = fixed_store.findings_for(tid)
    assert len(got["clusters"]) == 1
    assert abs(got["clusters"][0]["cluster_lat"] - SPOT[0]) < 1e-3
    assert got["clusters"][0]["dwell_seconds"] >= 600
    tf = [t for t in fixed_store.taskings_with_findings() if t["task_id"] == tid][0]
    assert tf["outcome"] == "seen"        # device seen, no local home AP
    assert tf["wigle_candidate"] is True
    assert fixed_store.get_tasking(tid)["status"] == "complete"

    gui.stop()
