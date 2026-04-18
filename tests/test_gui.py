"""Unit tests for gui/server.py — GUIServer class."""

import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch


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


if __name__ == "__main__":
    unittest.main()
