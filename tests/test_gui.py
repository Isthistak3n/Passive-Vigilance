"""Unit tests for gui/server.py — GUIServer class."""

import json
import queue
import threading
import unittest
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


class TestGUIServerAuth(unittest.TestCase):
    """GUI_TOKEN bearer-token auth enforcement."""

    def _make_gui(self, token: str):
        from gui.server import GUIServer
        with patch.dict("os.environ", {"GUI_TOKEN": token}):
            gui = GUIServer()
        return gui

    def test_auth_returns_401_when_token_required_and_none_provided(self):
        gui = self._make_gui("secret123")
        client = gui._app.test_client()
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_auth_returns_200_when_correct_bearer_token_provided(self):
        gui = self._make_gui("secret123")
        client = gui._app.test_client()
        response = client.get("/api/status", headers={"Authorization": "Bearer secret123"})
        self.assertEqual(response.status_code, 200)

    def test_auth_returns_200_when_no_token_configured(self):
        gui = self._make_gui("")
        client = gui._app.test_client()
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)


def _make_orch_stub():
    """Return a minimal PassiveVigilance instance stub for poll method tests."""
    import sys
    import types
    if "gps" not in sys.modules:
        fake_gps = types.ModuleType("gps")
        fake_gps.gps = object
        fake_gps.WATCH_ENABLE = 0
        sys.modules["gps"] = fake_gps
    import main as m
    orch = object.__new__(m.PassiveVigilance)
    orch._sensor_health = {"gps": True, "kismet": True, "adsb": True, "drone_rf": True}
    orch._degraded_log_counter = {"gps": 0, "kismet": 0, "adsb": 0, "drone_rf": 0}
    orch._stats = {"kismet_devices_seen": 0, "aircraft_seen": 0, "drone_detections": 0,
                   "alerts_sent": 0, "alerts_rate_limited": 0, "persistent_detections": 0}
    orch._current_fix = None
    orch.all_events = []
    orch.gui_server = None
    orch._write_session_summary = MagicMock()
    orch.kismet = MagicMock()
    orch.kismet.poll_devices = AsyncMock(return_value=[])
    orch.probe_analyzer = MagicMock()
    orch.probe_analyzer.analyze = MagicMock(return_value=[])
    orch.persistence = MagicMock()
    orch.persistence.update = MagicMock(return_value=[])
    orch._append_jsonl = MagicMock()
    orch._console_alert = MagicMock()
    from pathlib import Path
    import tempfile
    orch._session_dir = Path(tempfile.mkdtemp())
    orch.rate_limiter = MagicMock()
    orch.alert_backend = MagicMock()
    return orch


class TestOrchestratorIncrementalSummary(unittest.TestCase):
    """_write_session_summary() is called after each kismet poll on success."""

    def test_incremental_summary_called_after_kismet_poll(self):
        import asyncio
        import main as m  # noqa: F401 — ensure module imported
        orch = _make_orch_stub()
        orch.kismet.poll_devices = AsyncMock(return_value=[])

        async def _run():
            await orch._poll_kismet()

        asyncio.run(_run())
        orch._write_session_summary.assert_called_once()


class TestOrchestratorDegradedCounter(unittest.TestCase):
    """_degraded_log_counter emits WARNING every 10 consecutive failures."""

    def test_warning_logged_at_every_10th_failure(self):
        import asyncio
        import logging
        import main as m

        orch = _make_orch_stub()
        # Start already-degraded so the counter path (not reconnect) is exercised
        orch._sensor_health["kismet"] = False
        orch.kismet.poll_devices.side_effect = RuntimeError("connection refused")

        warning_count = 0

        class _Counter(logging.Handler):
            def emit(self, record):
                nonlocal warning_count
                if record.levelno == logging.WARNING and "still degraded" in record.getMessage():
                    warning_count += 1

        handler = _Counter()
        m.logger.addHandler(handler)
        try:
            async def _run_polls(n: int):
                for _ in range(n):
                    await orch._poll_kismet()

            asyncio.run(_run_polls(20))
        finally:
            m.logger.removeHandler(handler)

        self.assertEqual(warning_count, 2)


if __name__ == "__main__":
    unittest.main()
