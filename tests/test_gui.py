"""Unit tests for gui/server.py — GUIServer class."""

import json
import os
import queue
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class TestGUIServerImport(unittest.TestCase):
    """GUIServer must be importable even when Flask is absent."""

    def test_import_does_not_raise(self):
        from gui.server import GUIServer  # noqa: F401


class TestGUIServerInit(unittest.TestCase):

    def setUp(self):
        from gui.server import GUIServer
        self.gui = GUIServer(host="127.0.0.1", port=9999)

    def test_default_recent_lists_are_empty(self):
        self.assertEqual(self.gui._recent_wifi, [])
        self.assertEqual(self.gui._recent_aircraft, [])
        self.assertEqual(self.gui._recent_drone, [])
        self.assertEqual(self.gui._recent_alerts, [])
        self.assertEqual(self.gui._recent_nearby, [])

    def test_clients_list_is_empty_at_init(self):
        self.assertEqual(self.gui._clients, [])

    def test_host_and_port_stored(self):
        self.assertEqual(self.gui._host, "127.0.0.1")
        self.assertEqual(self.gui._port, 9999)


class TestGUIServerPushEvent(unittest.TestCase):

    def setUp(self):
        from gui.server import GUIServer
        self.gui = GUIServer()

    def test_push_wifi_appends_to_recent(self):
        ev = {"mac": "aa:bb:cc:dd:ee:ff", "score": 0.8, "alert_level": "likely"}
        self.gui.push_event("wifi", ev)
        self.assertEqual(len(self.gui._recent_wifi), 1)
        self.assertEqual(self.gui._recent_wifi[0]["mac"], "aa:bb:cc:dd:ee:ff")

    def test_push_wifi_preserves_ssid(self):
        ev = {"mac": "aa:bb:cc:dd:ee:ff", "ssid": "NETGEAR13", "score": 0.8}
        self.gui.push_event("wifi", ev)
        self.assertEqual(self.gui._recent_wifi[0]["ssid"], "NETGEAR13")

    def test_push_wifi_preserves_fingerprint(self):
        ev = {"mac": "aa:bb:cc:dd:ee:ff", "fingerprint": "ble-fp:abc123",
              "fingerprint_label": "Apple"}
        self.gui.push_event("wifi", ev)
        self.assertEqual(self.gui._recent_wifi[0]["fingerprint"], "ble-fp:abc123")
        self.assertEqual(self.gui._recent_wifi[0]["fingerprint_label"], "Apple")

    def test_push_aircraft_appends_to_recent(self):
        ev = {"icao": "abc123", "callsign": "BA001"}
        self.gui.push_event("aircraft", ev)
        self.assertEqual(len(self.gui._recent_aircraft), 1)

    def test_push_drone_appends_to_recent(self):
        ev = {"freq_mhz": 433.0, "power_db": -25.0}
        self.gui.push_event("drone", ev)
        self.assertEqual(len(self.gui._recent_drone), 1)

    def test_push_alert_appends_to_recent(self):
        ev = {"kind": "wifi", "title": "Test alert", "body": "Details"}
        self.gui.push_event("alert", ev)
        self.assertEqual(len(self.gui._recent_alerts), 1)

    def test_push_nearby_appends_to_recent(self):
        # _recent_nearby is only filled in mobile mode (fixed nodes don't serve /api/nearby)
        self.gui._node_mode = "mobile"
        ev = {"mac": "aa:bb:cc:dd:ee:ff", "name": "Phone", "last_signal": -55}
        self.gui.push_event("nearby", ev)
        self.assertEqual(len(self.gui._recent_nearby), 1)
        self.assertEqual(self.gui._recent_nearby[0]["last_signal"], -55)

    def test_push_nearby_same_mac_dedups_to_one_entry(self):
        self.gui._node_mode = "mobile"
        self.gui.push_event("nearby", {"mac": "aa:bb:cc:dd:ee:ff", "last_signal": -70})
        self.gui.push_event("nearby", {"mac": "aa:bb:cc:dd:ee:ff", "last_signal": -50})
        self.assertEqual(len(self.gui._recent_nearby), 1)
        self.assertEqual(self.gui._recent_nearby[0]["last_signal"], -50)

    def test_push_remote_id_appends_to_recent(self):
        self.gui.push_event("remote_id", {"uas_id": "UAS-1", "ua_type": "Multirotor"})
        self.assertEqual(len(self.gui._recent_remote_id), 1)
        self.assertEqual(self.gui._recent_remote_id[0]["uas_id"], "UAS-1")

    def test_push_remote_id_same_uas_dedups_to_one_entry(self):
        # A drone re-broadcasting must collapse to ONE cache entry (latest state).
        self.gui.push_event("remote_id", {"uas_id": "UAS-1", "drone_alt_m": 100})
        self.gui.push_event("remote_id", {"uas_id": "UAS-1", "drone_alt_m": 150})
        self.assertEqual(len(self.gui._recent_remote_id), 1)
        self.assertEqual(self.gui._recent_remote_id[0]["drone_alt_m"], 150)

    def test_push_aircraft_same_icao_dedups_to_one_entry(self):
        # A plane re-seen every poll must collapse to ONE cache entry (its
        # latest state), not one entry per sighting — /api/aircraft returns
        # one track per aircraft, not one row per detection.
        self.gui.push_event("aircraft", {"icao": "abc123", "altitude": 30000})
        self.gui.push_event("aircraft", {"icao": "abc123", "altitude": 31000})
        self.gui.push_event("aircraft", {"icao": "abc123", "altitude": 32000})
        self.assertEqual(len(self.gui._recent_aircraft), 1)
        self.assertEqual(self.gui._recent_aircraft[0]["altitude"], 32000)

    def test_push_aircraft_distinct_icaos_kept_separately(self):
        self.gui.push_event("aircraft", {"icao": "aaa111"})
        self.gui.push_event("aircraft", {"icao": "bbb222"})
        self.assertEqual(len(self.gui._recent_aircraft), 2)

    def test_push_wifi_same_mac_dedups_to_one_entry(self):
        self.gui.push_event("wifi", {"mac": "aa:bb:cc:dd:ee:ff", "score": 0.5})
        self.gui.push_event("wifi", {"mac": "aa:bb:cc:dd:ee:ff", "score": 0.9})
        self.assertEqual(len(self.gui._recent_wifi), 1)
        self.assertEqual(self.gui._recent_wifi[0]["score"], 0.9)

    def test_push_event_broadcasts_to_clients(self):
        client_q: queue.Queue = queue.Queue()
        with self.gui._clients_lock:
            self.gui._clients.append(client_q)
        ev = {"mac": "de:ad:be:ef:00:01", "score": 0.5}
        self.gui.push_event("wifi", ev)
        msg = client_q.get_nowait()
        data = json.loads(msg)
        self.assertEqual(data["type"], "wifi")
        self.assertEqual(data["mac"], "de:ad:be:ef:00:01")

    def test_push_event_removes_full_client_queues(self):
        # A queue with maxsize=1 that is already full
        full_q: queue.Queue = queue.Queue(maxsize=1)
        full_q.put("existing")
        with self.gui._clients_lock:
            self.gui._clients.append(full_q)
        # Should not raise and should remove the dead client
        self.gui.push_event("wifi", {"mac": "xx:xx:xx:xx:xx:xx"})
        with self.gui._clients_lock:
            self.assertNotIn(full_q, self.gui._clients)

    def test_recent_list_capped_at_max(self):
        from gui.server import _MAX_RECENT
        for i in range(_MAX_RECENT + 10):
            self.gui.push_event("drone", {"freq_mhz": float(i)})
        self.assertLessEqual(len(self.gui._recent_drone), _MAX_RECENT)

    def test_push_event_thread_safe(self):
        """Concurrent push_event calls from multiple threads must not raise."""
        errors = []

        def _push():
            try:
                for _ in range(50):
                    self.gui.push_event("wifi", {"mac": "aa:bb:cc:dd:ee:ff"})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_push) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestGUIServerStop(unittest.TestCase):

    def setUp(self):
        from gui.server import GUIServer
        self.gui = GUIServer()

    def test_stop_sends_none_sentinel_to_clients(self):
        client_q: queue.Queue = queue.Queue()
        with self.gui._clients_lock:
            self.gui._clients.append(client_q)
        self.gui.stop()
        sentinel = client_q.get_nowait()
        self.assertIsNone(sentinel)

    def test_stop_clears_client_list(self):
        with self.gui._clients_lock:
            self.gui._clients.append(queue.Queue())
        self.gui.stop()
        self.assertEqual(self.gui._clients, [])


class TestGUIServerStartWithoutFlask(unittest.TestCase):
    """start() should return False gracefully when Flask is not importable."""

    def test_start_returns_false_when_app_is_none(self):
        from gui.server import GUIServer
        gui = GUIServer()
        gui._app = None  # simulate missing Flask
        result = gui.start()
        self.assertFalse(result)


# A realistic .env with comments, blank lines, and fake secrets. The toggle must
# touch ONLY the NODE_MODE line and leave everything else byte-for-byte intact.
FIXTURE_ENV = (
    "# Passive Vigilance config\n"
    "LOG_LEVEL=INFO\n"
    "\n"
    "# Detection mode\n"
    "NODE_MODE=mobile\n"
    "\n"
    "# secrets — must never be touched\n"
    "KISMET_API_KEY=KISMET_FAKE_SECRET_abc123\n"
    "NTFY_TOPIC=my-private-topic\n"
    "WIGLE_API_KEY=wigle-fake-xyz\n"
    "\n"
    "GUI_TOKEN=\n"
)


class TestSurgicalEnvWrite(unittest.TestCase):
    """write_node_mode / read_node_mode must be surgical and crash-safe."""

    def _env(self, content=FIXTURE_ENV):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, ".env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        return p

    def test_read_node_mode_returns_value(self):
        from gui.server import read_node_mode
        self.assertEqual(read_node_mode(self._env()), "mobile")

    def test_read_node_mode_absent_returns_empty(self):
        from gui.server import read_node_mode
        p = self._env("LOG_LEVEL=INFO\nKISMET_API_KEY=x\n")
        self.assertEqual(read_node_mode(p), "")

    def test_replace_preserves_all_other_lines_and_secret(self):
        from gui.server import write_node_mode
        p = self._env()
        write_node_mode("fixed", p)
        with open(p, encoding="utf-8") as fh:
            after = fh.read()

        # Only the NODE_MODE line changed; every other line byte-identical.
        before_lines = FIXTURE_ENV.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        self.assertEqual(len(before_lines), len(after_lines))
        for b, a in zip(before_lines, after_lines):
            if b.startswith("NODE_MODE="):
                self.assertEqual(a, "NODE_MODE=fixed\n")
            else:
                self.assertEqual(a, b)  # byte-identical

        # Secrets explicitly survived.
        self.assertIn("KISMET_API_KEY=KISMET_FAKE_SECRET_abc123\n", after)
        self.assertIn("NTFY_TOPIC=my-private-topic\n", after)
        self.assertIn("WIGLE_API_KEY=wigle-fake-xyz\n", after)

    def test_append_when_node_mode_absent(self):
        from gui.server import read_node_mode, write_node_mode
        p = self._env("LOG_LEVEL=INFO\nKISMET_API_KEY=keepme\n")
        write_node_mode("fixed", p)
        with open(p, encoding="utf-8") as fh:
            after = fh.read()
        self.assertIn("KISMET_API_KEY=keepme\n", after)  # untouched
        self.assertTrue(after.endswith("NODE_MODE=fixed\n"))
        self.assertEqual(read_node_mode(p), "fixed")

    def test_does_not_match_commented_node_mode(self):
        from gui.server import write_node_mode
        p = self._env("# NODE_MODE=mobile\nKISMET_API_KEY=keepme\n")
        write_node_mode("fixed", p)
        with open(p, encoding="utf-8") as fh:
            after = fh.read()
        self.assertIn("# NODE_MODE=mobile\n", after)      # comment preserved
        self.assertTrue(after.endswith("NODE_MODE=fixed\n"))  # real value appended


class TestModeEndpoint(unittest.TestCase):
    """POST/GET /api/mode — control endpoint with stricter token rules."""

    def _env(self, content=FIXTURE_ENV):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, ".env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        return p

    def _client(self, token, env_path):
        # GUI_TOKEN is read in __init__, so patch the env before constructing.
        with patch.dict(os.environ, {"GUI_TOKEN": token}):
            from gui.server import GUIServer
            gui = GUIServer(env_path=env_path)
        if gui.app is None:
            self.skipTest("Flask not installed — skipping mode-endpoint tests")
        return gui.app.test_client()

    def _read(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    # ---- valid writes (token configured + supplied) ----

    def test_post_valid_fixed_writes_env_and_requires_restart(self):
        p = self._env()
        client = self._client("secret", p)
        resp = client.post("/api/mode?token=secret", json={"mode": "fixed"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["mode"], "fixed")
        self.assertTrue(body["restart_required"])
        self.assertIn("RESTART REQUIRED", body["message"])
        self.assertIn("NODE_MODE=fixed\n", self._read(p))

    def test_post_valid_mobile_writes_env(self):
        p = self._env("NODE_MODE=fixed\nKISMET_API_KEY=keep\n")
        client = self._client("secret", p)
        resp = client.post("/api/mode?token=secret", json={"mode": "mobile"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("NODE_MODE=mobile\n", self._read(p))
        self.assertIn("KISMET_API_KEY=keep\n", self._read(p))

    def test_post_invalid_value_400_no_write(self):
        p = self._env()
        before = self._read(p)
        client = self._client("secret", p)
        resp = client.post("/api/mode?token=secret", json={"mode": "wardrive"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._read(p), before)  # unchanged

    # ---- no GUI_TOKEN configured → 403, no write ----

    def test_post_without_token_configured_403_no_write(self):
        p = self._env()
        before = self._read(p)
        client = self._client("", p)  # GUI_TOKEN empty
        resp = client.post("/api/mode", json={"mode": "fixed"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("GUI_TOKEN", resp.get_json()["error"])
        self.assertEqual(self._read(p), before)  # no write performed

    # ---- GUI_TOKEN configured but caller unauthenticated → 401 (existing auth) ----

    def test_post_missing_token_401_no_write(self):
        p = self._env()
        before = self._read(p)
        client = self._client("secret", p)
        resp = client.post("/api/mode", json={"mode": "fixed"})  # no ?token=
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._read(p), before)

    def test_post_wrong_token_401_no_write(self):
        p = self._env()
        before = self._read(p)
        client = self._client("secret", p)
        resp = client.post("/api/mode?token=nope", json={"mode": "fixed"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._read(p), before)

    # ---- GET reports current mode + whether control is enabled ----

    def test_get_reports_mode_and_control_enabled_with_token(self):
        p = self._env()
        client = self._client("secret", p)
        body = client.get("/api/mode?token=secret").get_json()
        self.assertEqual(body["mode"], "mobile")
        self.assertTrue(body["control_enabled"])

    def test_get_control_disabled_when_no_token(self):
        p = self._env()
        client = self._client("", p)
        body = client.get("/api/mode").get_json()
        self.assertEqual(body["mode"], "mobile")
        self.assertFalse(body["control_enabled"])

    def test_read_only_endpoints_stay_open_without_token(self):
        # Guard: the new rule must NOT have tightened read-only routes.
        p = self._env()
        client = self._client("", p)
        for route in ("/api/status", "/api/wifi", "/api/aircraft",
                      "/api/drone", "/api/alerts", "/api/nearby"):
            self.assertEqual(client.get(route).status_code, 200, route)


class TestStatusSensorHealthHonesty(unittest.TestCase):
    """/api/status must not report a disabled sensor as healthy.

    sensor_health is initialised all-True and only flipped False on a poll that
    raises; a disabled module never polls, so without gating it reports "healthy"
    forever (AIS/ACARS showing online while off). The endpoint gates health by
    modules_active so the raw API is honest.
    """

    def _client(self, orch):
        from gui.server import GUIServer
        gui = GUIServer(orchestrator=orch)
        if gui.app is None:
            self.skipTest("Flask not installed — skipping status tests")
        return gui.app.test_client()

    def test_disabled_sensor_reported_unhealthy(self):
        orch = SimpleNamespace(
            _sensor_health={"adsb": True, "kismet": True, "ais": True,
                            "acars": True, "sdr": True},
            _modules_active={"adsb": True, "kismet": True, "ais": False,
                             "acars": False},
        )
        health = self._client(orch).get("/api/status").get_json()["sensor_health"]
        # Active sensors keep their reported health.
        self.assertTrue(health["adsb"])
        self.assertTrue(health["kismet"])
        # Disabled ones are forced unhealthy, not the initialised True.
        self.assertFalse(health["ais"])
        self.assertFalse(health["acars"])
        # A health key with no modules_active entry (e.g. "sdr") is left as-is.
        self.assertTrue(health["sdr"])

    def test_active_but_broken_sensor_stays_unhealthy(self):
        # Gating must not resurrect a genuinely failed-but-active sensor.
        orch = SimpleNamespace(
            _sensor_health={"kismet": False},
            _modules_active={"kismet": True},
        )
        health = self._client(orch).get("/api/status").get_json()["sensor_health"]
        self.assertFalse(health["kismet"])


class TestGUIServerNearbyEndpoint(unittest.TestCase):
    """/api/nearby — live proximity feed for the mobile GUI."""

    def setUp(self):
        from gui.server import GUIServer
        self.gui = GUIServer()
        # /api/nearby is mobile-only; _recent_nearby is only populated in mobile mode
        self.gui._node_mode = "mobile"
        if self.gui.app is None:
            self.skipTest("Flask not installed — skipping nearby-endpoint tests")
        self.client = self.gui.app.test_client()

    def test_empty_by_default(self):
        self.assertEqual(self.client.get("/api/nearby").get_json(), [])

    def test_returns_pushed_devices(self):
        self.gui.push_event("nearby", {"mac": "aa:bb:cc:dd:ee:ff", "last_signal": -60})
        body = self.client.get("/api/nearby").get_json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["mac"], "aa:bb:cc:dd:ee:ff")


class TestGUIServerTemplateSelection(unittest.TestCase):
    """index() serves mobile.html when NODE_MODE=mobile, index.html otherwise."""

    def _env(self, mode):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, ".env")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"NODE_MODE={mode}\n")
        return p

    def _client(self, mode):
        from gui.server import GUIServer
        gui = GUIServer(env_path=self._env(mode))
        if gui.app is None:
            self.skipTest("Flask not installed — skipping template-selection tests")
        return gui.app.test_client()

    def test_mobile_mode_serves_mobile_template(self):
        resp = self._client("mobile").get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'data-tab="nearby"', resp.data)
        self.assertNotIn(b'id="map"', resp.data)

    def test_fixed_mode_serves_index_template(self):
        resp = self._client("fixed").get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="map"', resp.data)
        self.assertNotIn(b'data-tab="nearby"', resp.data)


class TestGUIServerAuth(unittest.TestCase):
    """GUI_TOKEN bearer-token auth enforcement."""

    def _make_client(self, token: str):
        """Create a GUIServer with the given token and return its Flask test client.

        Skips the test if Flask is not installed (app property returns None).
        """
        from gui.server import GUIServer
        with patch.dict("os.environ", {"GUI_TOKEN": token}):
            gui = GUIServer()
        if gui.app is None:
            self.skipTest("Flask not installed — skipping auth tests")
        return gui.app.test_client()

    def test_auth_returns_401_when_token_required_and_none_provided(self):
        client = self._make_client("secret123")
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_auth_returns_200_when_correct_bearer_token_provided(self):
        client = self._make_client("secret123")
        response = client.get("/api/status", headers={"Authorization": "Bearer secret123"})
        self.assertEqual(response.status_code, 200)

    def test_auth_returns_200_when_no_token_configured(self):
        client = self._make_client("")
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)


def _make_orch_stub():
    """Return a minimal SensorOrchestrator stub for poll method tests."""
    import sys
    import types
    import tempfile
    from pathlib import Path
    from modules.orchestrator import SensorOrchestrator

    if "gps" not in sys.modules:
        fake_gps = types.ModuleType("gps")
        fake_gps.gps = object
        fake_gps.WATCH_ENABLE = 0
        sys.modules["gps"] = fake_gps

    from datetime import datetime, timezone
    from asyncio import Event

    stop_event = Event()
    modules_active = {"gps": False, "kismet": False, "adsb": False, "drone_rf": False, "sdr_coordinator": False}
    sdr_coordinator_mock = MagicMock()
    sdr_coordinator_mock.healthy = True
    sdr_coordinator_mock.current_owner = "none"
    sdr_coordinator_mock.healthy = True

    from modules.sdr_manager import SDRMode

    orch = SensorOrchestrator(
        gps=MagicMock(), kismet=MagicMock(), adsb=MagicMock(),
        drone_rf=MagicMock(), sdr_coordinator=sdr_coordinator_mock,
        alert_backend=MagicMock(), rate_limiter=MagicMock(),
        persistence=MagicMock(), probe_analyzer=MagicMock(),
        gui_server=None,
        session_id="20260101_120000",
        session_start=datetime.now(timezone.utc),
        session_dir=Path(tempfile.mkdtemp()),
        sdr_mode=SDRMode.AUTO,
        stop_event=stop_event,
        gps_poll_interval=1, adsb_poll_interval=5,
        kismet_poll_interval=30, drone_poll_interval=5,
        health_banner_interval=300, max_reconnect_attempts=3,
        reconnect_interval=5, modules_active=modules_active,
    )
    orch.kismet.poll_devices = AsyncMock(return_value=[])
    orch.probe_analyzer.analyze = MagicMock(return_value=[])
    orch.persistence.update = MagicMock(return_value=[])
    orch._append_jsonl = MagicMock()
    orch._console_alert = MagicMock()
    orch._write_session_summary = MagicMock()
    return orch


class TestOrchestratorIncrementalSummary(unittest.TestCase):
    """_write_session_summary() is called after each kismet poll on success."""

    def test_incremental_summary_called_after_kismet_poll(self):
        import asyncio
        so = _make_orch_stub()
        so.kismet.poll_devices = AsyncMock(return_value=[])

        async def _run():
            await so._poll_kismet()

        asyncio.run(_run())
        so._write_session_summary.assert_called_once()


class TestOrchestratorDegradedCounter(unittest.TestCase):
    """_degraded_log_counter emits WARNING every 10 consecutive failures."""

    def test_warning_logged_at_every_10th_failure(self):
        import asyncio
        import logging
        import modules.orchestrator as mo

        so = _make_orch_stub()
        # Start already-degraded so the counter path (not reconnect) is exercised
        so._sensor_health["kismet"] = False
        so.kismet.poll_devices.side_effect = RuntimeError("connection refused")

        warning_count = 0

        class _Counter(logging.Handler):
            def emit(self, record):
                nonlocal warning_count
                if record.levelno == logging.WARNING and "still degraded" in record.getMessage():
                    warning_count += 1

        handler = _Counter()
        mo.logger.addHandler(handler)
        try:
            async def _run_polls(n: int):
                for _ in range(n):
                    await so._poll_kismet()

            asyncio.run(_run_polls(20))
        finally:
            mo.logger.removeHandler(handler)

        self.assertEqual(warning_count, 2)


if __name__ == "__main__":
    unittest.main()


class TestGUIServerBindRetry(unittest.TestCase):
    """The Flask bind retries so a fast restart doesn't leave the GUI silently dead."""

    def _server(self):
        from gui.server import GUIServer
        return GUIServer(host="127.0.0.1", port=9999)

    def test_bind_retries_then_succeeds(self):
        gui = self._server()
        gui._app = MagicMock()
        # First bind fails (port still held), second returns (clean shutdown).
        gui._app.run.side_effect = [OSError("address in use"), None]
        with patch.dict(os.environ, {"GUI_BIND_RETRIES": "5", "GUI_BIND_RETRY_SECONDS": "0"}):
            gui._serve_with_retry()
        self.assertEqual(gui._app.run.call_count, 2)

    def test_bind_gives_up_after_attempts_without_raising(self):
        gui = self._server()
        gui._app = MagicMock()
        gui._app.run.side_effect = OSError("address in use")
        with patch.dict(os.environ, {"GUI_BIND_RETRIES": "3", "GUI_BIND_RETRY_SECONDS": "0"}):
            with self.assertLogs("gui.server", level="ERROR") as cm:
                gui._serve_with_retry()   # must NOT raise out of the daemon thread
        self.assertEqual(gui._app.run.call_count, 3)
        self.assertTrue(any("could not bind" in m for m in cm.output))

    def test_bind_succeeds_first_try(self):
        gui = self._server()
        gui._app = MagicMock()
        gui._app.run.return_value = None
        with patch.dict(os.environ, {"GUI_BIND_RETRIES": "5", "GUI_BIND_RETRY_SECONDS": "0"}):
            gui._serve_with_retry()
        self.assertEqual(gui._app.run.call_count, 1)


class TestGUIStatusScoring(unittest.TestCase):
    """/api/status surfaces the scoring engine's baseline state for the header."""

    def _client_with_scoring(self, status_value=None, status_error=None):
        from gui.server import GUIServer
        orch = MagicMock()
        orch.session_id = "20260607_000000"
        orch._sensor_health = {"gps": True, "kismet": True}
        orch._stats = {}
        orch._current_fix = None
        if status_error is not None:
            orch.persistence.status.side_effect = status_error
        else:
            orch.persistence.status.return_value = status_value
        gui = GUIServer(orchestrator=orch)
        if gui.app is None:
            self.skipTest("Flask not installed")
        return gui.app.test_client()

    def test_status_includes_fixed_baseline(self):
        client = self._client_with_scoring({
            "mode": "fixed", "learning": True,
            "freeze_time": "2026-06-09T22:55:03+00:00", "baseline_devices": 500,
        })
        body = client.get("/api/status").get_json()
        self.assertEqual(body["scoring"]["mode"], "fixed")
        self.assertTrue(body["scoring"]["learning"])
        self.assertEqual(body["scoring"]["baseline_devices"], 500)

    def test_status_guards_scoring_failure(self):
        client = self._client_with_scoring(status_error=RuntimeError("boom"))
        resp = client.get("/api/status")
        self.assertEqual(resp.status_code, 200)        # never breaks /api/status
        self.assertIsNone(resp.get_json()["scoring"])  # guarded -> null


class TestGUIStatusModulesActive(unittest.TestCase):
    """/api/status exposes modules_active so the dashboard shows a disabled
    sensor (e.g. DroneRF off) as off, not falsely healthy."""

    def test_status_exposes_modules_active(self):
        from gui.server import GUIServer
        orch = MagicMock()
        orch.session_id = "x"
        orch._sensor_health = {"drone_rf": True, "kismet": True}
        orch._modules_active = {"drone_rf": False, "kismet": True}
        orch._stats = {}
        orch._current_fix = None
        orch.persistence.status.return_value = None
        gui = GUIServer(orchestrator=orch)
        if gui.app is None:
            self.skipTest("Flask not installed")
        body = gui.app.test_client().get("/api/status").get_json()
        # sensor_health is now gated by modules_active server-side: an inactive
        # sensor can't report healthy (it used to stay True and rely on the
        # dashboard to combine the two). modules_active is still exposed raw.
        self.assertFalse(body["sensor_health"]["drone_rf"])
        self.assertTrue(body["sensor_health"]["kismet"])
        self.assertFalse(body["modules_active"]["drone_rf"])
        self.assertTrue(body["modules_active"]["kismet"])


class _FakeOrch:
    """Minimal orchestrator stand-in exposing only ``_session_dir`` (a real Path),
    so GUIServer's durable-history reads resolve a sessions root without MagicMock
    polluting Path()."""

    def __init__(self, session_dir):
        self._session_dir = session_dir


class TestGUIServerDurableHistory(unittest.TestCase):
    """P5 — panels rebuild from on-disk session logs, surviving refresh/restart."""

    def setUp(self):
        from gui.server import GUIServer
        self._tmp = tempfile.mkdtemp()
        self._root = os.path.join(self._tmp, "sessions")
        os.makedirs(self._root)
        self.GUIServer = GUIServer

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _session(self, name):
        d = os.path.join(self._root, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _write(self, session_name, filename, records):
        from pathlib import Path
        path = Path(self._session(session_name)) / filename
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def _gui_for(self, current_session):
        from pathlib import Path
        orch = _FakeOrch(Path(self._session(current_session)))
        return self.GUIServer(orchestrator=orch)

    # --- _read_jsonl_tail -------------------------------------------------

    def test_read_jsonl_tail_bounds_and_skips_torn_line(self):
        from pathlib import Path
        path = self._write("20260101_000000", "events.jsonl",
                           [{"i": n} for n in range(5)])
        # Append a torn final line (a power-cut leftover).
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"i": 5, "part')
        # maxlen=3 keeps the last 3 lines (i=3, i=4, torn); the torn one is
        # skipped on parse, leaving [3, 4] — proves both the bound and tolerance.
        out = self.GUIServer._read_jsonl_tail(Path(path), 3)
        self.assertEqual([r["i"] for r in out], [3, 4])

    def test_read_jsonl_tail_missing_file_is_empty(self):
        from pathlib import Path
        out = self.GUIServer._read_jsonl_tail(Path(self._tmp) / "nope.jsonl", 10)
        self.assertEqual(out, [])

    # --- _history ---------------------------------------------------------

    def test_history_reads_current_session(self):
        self._write("20260101_000000", "drone.jsonl",
                    [{"freq_mhz": 433.0}, {"freq_mhz": 915.0}])
        gui = self._gui_for("20260101_000000")
        hist = gui._history("drone.jsonl", 500)
        self.assertEqual([r["freq_mhz"] for r in hist], [433.0, 915.0])

    def test_history_accumulates_across_sessions_newest_first(self):
        self._write("20260101_000000", "drone.jsonl", [{"freq_mhz": 1.0}])  # older
        self._write("20260102_000000", "drone.jsonl", [{"freq_mhz": 2.0}])  # newer
        gui = self._gui_for("20260102_000000")
        hist = gui._history("drone.jsonl", 500)
        # Oldest -> newest across sessions: old session's event precedes new one.
        self.assertEqual([r["freq_mhz"] for r in hist], [1.0, 2.0])

    def test_history_dedups_by_key_keeping_latest(self):
        # Same MAC in an older and a newer session — keep the newer record.
        self._write("20260101_000000", "events.jsonl",
                    [{"mac": "aa", "score": 0.1}])
        self._write("20260102_000000", "events.jsonl",
                    [{"mac": "aa", "score": 0.9}, {"mac": "bb", "score": 0.5}])
        gui = self._gui_for("20260102_000000")
        hist = gui._history("events.jsonl", 500, dedup_key="mac")
        by_mac = {r["mac"]: r["score"] for r in hist}
        self.assertEqual(by_mac, {"aa": 0.9, "bb": 0.5})
        self.assertEqual(len(hist), 2)

    def test_history_returns_none_without_orchestrator(self):
        gui = self.GUIServer()  # no orchestrator -> caller uses cache
        self.assertIsNone(gui._history("events.jsonl", 500))

    # --- endpoints --------------------------------------------------------

    def test_api_alerts_serves_disk_history(self):
        self._write("20260101_000000", "alerts.jsonl",
                    [{"kind": "wifi", "title": "A", "body": "x"},
                     {"kind": "drone", "title": "B", "body": "y"}])
        gui = self._gui_for("20260101_000000")
        if gui.app is None:
            self.skipTest("Flask not installed")
        body = gui.app.test_client().get("/api/alerts").get_json()
        self.assertEqual([a["title"] for a in body], ["A", "B"])

    def test_api_wifi_dedups_disk_history(self):
        self._write("20260101_000000", "events.jsonl",
                    [{"mac": "aa", "score": 0.2}, {"mac": "aa", "score": 0.7}])
        gui = self._gui_for("20260101_000000")
        if gui.app is None:
            self.skipTest("Flask not installed")
        body = gui.app.test_client().get("/api/wifi").get_json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["score"], 0.7)


class TestGUIServerRemoteIDEndpoint(unittest.TestCase):
    """/api/remote_id — current-sky lens served from the live per-UAS index (P6)."""

    def test_api_remote_id_serves_current_index(self):
        from gui.server import GUIServer

        class _Orch:
            def current_remote_id(self):
                return [{"uas_id": "UAS-1", "ua_type": "Multirotor"}]

        gui = GUIServer(orchestrator=_Orch())
        if gui.app is None:
            self.skipTest("Flask not installed")
        body = gui.app.test_client().get("/api/remote_id").get_json()
        self.assertEqual([e["uas_id"] for e in body], ["UAS-1"])

    def test_api_remote_id_falls_back_to_cache(self):
        from gui.server import GUIServer
        gui = GUIServer()  # no orchestrator -> empty cache, still 200
        if gui.app is None:
            self.skipTest("Flask not installed")
        resp = gui.app.test_client().get("/api/remote_id")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])


class TestGUIServerIndexNoCache(unittest.TestCase):
    """The HTML doc must revalidate so it never drifts behind the no-cache app.js
    (a stale cached page + fresh JS broke the dashboard once)."""

    def test_index_sets_no_cache(self):
        from gui.server import GUIServer
        gui = GUIServer()
        if gui.app is None:
            self.skipTest("Flask not installed")
        resp = gui.app.test_client().get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-cache", resp.headers.get("Cache-Control", ""))


class TestGUIServerAircraftEndpoint(unittest.TestCase):
    """/api/aircraft merges the durable on-disk log (so the table survives a refresh
    AND a restart) with the live current-sky index overlaid (fresh positions for
    what's overhead now)."""

    def _client(self, disk_records, live_records):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        sdir = os.path.join(d, "data", "sessions", "20260101_000000")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "aircraft.jsonl"), "w", encoding="utf-8") as fh:
            for rec in disk_records:
                fh.write(json.dumps(rec) + "\n")

        class _Orch:
            _session_dir = sdir

            def current_aircraft(self):
                return live_records

        from gui.server import GUIServer
        gui = GUIServer(orchestrator=_Orch())
        if gui.app is None:
            self.skipTest("Flask not installed")
        return gui.app.test_client()

    def test_disk_aircraft_survive_when_sky_empty(self):
        # On disk but no longer overhead (empty live index) -> still in the table.
        client = self._client(
            [{"icao": "ABC123", "event_type": "aircraft", "lat": 1.0, "lon": 2.0,
              "timestamp": "2026-01-01T00:00:00+00:00"}],
            live_records=[],
        )
        body = client.get("/api/aircraft").get_json()
        self.assertIn("ABC123", {r["icao"] for r in body})

    def test_live_overlay_wins_over_stale_disk(self):
        client = self._client(
            [{"icao": "DEF456", "event_type": "aircraft", "lat": 0.0, "lon": 0.0,
              "timestamp": "2026-01-01T00:00:00+00:00"}],
            live_records=[{"icao": "DEF456", "event_type": "aircraft", "lat": 51.5,
                           "lon": -0.1, "timestamp": "2026-01-01T01:00:00+00:00"}],
        )
        row = [r for r in client.get("/api/aircraft").get_json()
               if r["icao"] == "DEF456"][0]
        self.assertEqual(row["lat"], 51.5)  # fresh live position, not the stale disk 0.0

    def test_union_of_past_and_present(self):
        client = self._client(
            [{"icao": "OLD", "event_type": "aircraft",
              "timestamp": "2026-01-01T00:00:00+00:00"}],
            live_records=[{"icao": "NEW", "event_type": "aircraft",
                           "timestamp": "2026-01-01T01:00:00+00:00"}],
        )
        self.assertEqual(
            {r["icao"] for r in client.get("/api/aircraft").get_json()},
            {"OLD", "NEW"})
