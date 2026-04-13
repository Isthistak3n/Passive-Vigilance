"""Unit tests for modules/persistence.py — PersistenceEngine and DetectionEvent."""

import unittest
from datetime import datetime, timedelta, timezone

import modules.persistence  # noqa: F401 — ensure module importable

from modules.persistence import DetectionEvent, PersistenceEngine


def _obs(minutes_ago: float, lat=None, lon=None, signal=-60.0, manuf="TestCo", dtype="Wi-Fi Device"):
    """Build a minimal observation dict at *minutes_ago* minutes before now."""
    return {
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "gps_lat":   lat,
        "gps_lon":   lon,
        "signal":    signal,
        "manuf":     manuf,
        "type":      dtype,
        "name":      "",
    }


MAC_A = "aa:bb:cc:dd:ee:ff"
MAC_B = "11:22:33:44:55:66"

# London/Paris reference coordinates
LON_LAT, LON_LON = 51.5074, -0.1278
PAR_LAT, PAR_LON = 48.8566,  2.3522


class TestScoreDeviceSingleObservation(unittest.TestCase):

    def test_score_is_zero_for_single_observation(self):
        """score_device() must return 0.0 when fewer than 2 observations exist."""
        pe = PersistenceEngine()
        pe._observations[MAC_A] = [_obs(1)]
        self.assertEqual(pe.score_device(MAC_A), 0.0)


class TestScoreDeviceMultipleWindows(unittest.TestCase):

    def test_score_is_nonzero_for_device_seen_in_all_windows(self):
        """score_device() returns a positive score when device spans all time windows."""
        pe = PersistenceEngine(window_minutes=[5, 10, 15, 20], poll_interval_seconds=240)
        # One observation in each window band: 2, 7, 12, 17 minutes ago
        pe._observations[MAC_A] = [_obs(m) for m in [2, 7, 12, 17]]
        score = pe.score_device(MAC_A)
        self.assertGreater(score, 0.3)

    def test_score_reflects_temporal_weight_correctly(self):
        """With device seen only in the two oldest windows, temporal contribution ≈ 0.175."""
        pe = PersistenceEngine(window_minutes=[5, 10, 15, 20], poll_interval_seconds=30)
        # 12 and 13 minutes ago → inside the 15-min and 20-min windows, but NOT 5-min or 10-min
        # signal=None eliminates that component so only temporal + small frequency contribute
        pe._observations[MAC_A] = [_obs(12, signal=None), _obs(13, signal=None)]
        score = pe.score_device(MAC_A)
        # temporal = 2/4 = 0.5 → contribution = 0.175
        # frequency = 2/40 = 0.05 → contribution ≈ 0.01
        # signal = 0.0 (None), location = 0.0 (no GPS)
        # expected ≈ 0.185
        self.assertAlmostEqual(score, 0.185, delta=0.02)


class TestLocationScore(unittest.TestCase):

    def _pe_with_gps_obs(self, coords):
        """Return a PersistenceEngine with observations at the given (lat, lon) tuples."""
        pe = PersistenceEngine(poll_interval_seconds=30)
        pe._observations[MAC_A] = [
            _obs(i * 2, lat=lat, lon=lon)
            for i, (lat, lon) in enumerate(coords)
        ]
        return pe

    def test_location_score_zero_for_single_cluster(self):
        """location score is 0.0 when all observations are at the same GPS location."""
        coords = [(51.5074, -0.1278)] * 5
        pe = self._pe_with_gps_obs(coords)
        components = pe._compute_score_components(MAC_A)
        self.assertEqual(components["location"], 0.0)

    def test_location_score_half_for_two_clusters(self):
        """location score is 0.5 when observations span exactly two distinct clusters."""
        # Location A and location B ~556 m apart (0.005° latitude ≈ 556 m)
        coords = [(51.5074, -0.1278)] * 3 + [(51.5124, -0.1278)] * 3
        pe = self._pe_with_gps_obs(coords)
        components = pe._compute_score_components(MAC_A)
        self.assertEqual(components["location"], 0.5)

    def test_location_score_one_for_three_clusters(self):
        """location score is 1.0 when observations span three or more clusters."""
        coords = (
            [(51.5074, -0.1278)] * 2   # cluster A
            + [(51.5174, -0.1278)] * 2  # cluster B (~1.1 km north)
            + [(51.5074, -0.1500)] * 2  # cluster C (~1.3 km west)
        )
        pe = self._pe_with_gps_obs(coords)
        components = pe._compute_score_components(MAC_A)
        self.assertEqual(components["location"], 1.0)


class TestHaversine(unittest.TestCase):

    def test_haversine_london_to_paris(self):
        """haversine() should return ~340 km between London and Paris."""
        pe = PersistenceEngine()
        dist_m = pe.haversine(LON_LAT, LON_LON, PAR_LAT, PAR_LON)
        dist_km = dist_m / 1000
        self.assertAlmostEqual(dist_km, 341, delta=10)

    def test_haversine_same_point_is_zero(self):
        """haversine() returns 0.0 when both points are identical."""
        pe = PersistenceEngine()
        self.assertAlmostEqual(pe.haversine(51.5074, -0.1278, 51.5074, -0.1278), 0.0, places=3)


class TestClusterLocations(unittest.TestCase):

    def test_cluster_groups_nearby_points_into_one_cluster(self):
        """Points within 100 m of each other should form a single cluster."""
        pe = PersistenceEngine()
        # 5 points within ~44 m of each other (0.0001° lat ≈ 11 m)
        obs = [{"gps_lat": 51.5074 + i * 0.0001, "gps_lon": -0.1278} for i in range(5)]
        clusters = pe.cluster_locations(obs, threshold_meters=100.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["count"], 5)

    def test_cluster_separates_distant_points_into_two_clusters(self):
        """Points > 100 m apart should form separate clusters."""
        pe = PersistenceEngine()
        # Group A at 51.5074 N, group B at 51.5124 N (~556 m north)
        obs_a = [{"gps_lat": 51.5074, "gps_lon": -0.1278} for _ in range(3)]
        obs_b = [{"gps_lat": 51.5124, "gps_lon": -0.1278} for _ in range(3)]
        clusters = pe.cluster_locations(obs_a + obs_b, threshold_meters=100.0)
        self.assertEqual(len(clusters), 2)


class TestUpdateReturnValues(unittest.TestCase):

    def test_update_returns_event_when_threshold_crossed(self):
        """update() returns a DetectionEvent when a device's score crosses alert_threshold."""
        # Low threshold + pre-populated obs across all 4 windows + adequate frequency
        pe = PersistenceEngine(alert_threshold=0.3, poll_interval_seconds=240)
        # Pre-populate 4 observations: one per window band, no GPS (bypasses min_locations gate)
        pe._observations[MAC_A] = [_obs(m) for m in [2, 7, 12, 17]]
        # update() adds a 5th observation; with poll_interval=240 s and max_window=20 min:
        # expected = 20*60/240 = 5; frequency = 5/5 = 1.0
        devices = [{
            "macaddr":     MAC_A,
            "manuf":       "Apple",
            "type":        "Wi-Fi Device",
            "name":        "test",
            "last_signal": -60,
        }]
        events = pe.update(devices, gps_fix=None)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].mac, MAC_A)
        self.assertIsInstance(events[0], DetectionEvent)

    def test_update_returns_empty_when_threshold_not_crossed(self):
        """update() returns [] for a device seen for the first time."""
        pe = PersistenceEngine(alert_threshold=0.7)
        devices = [{"macaddr": MAC_A, "manuf": "", "type": "", "name": "", "last_signal": None}]
        events = pe.update(devices, gps_fix=None)
        self.assertEqual(events, [])


class TestPurgeObservations(unittest.TestCase):

    def test_purge_removes_old_observations(self):
        """purge_old_observations() removes observations older than max_age_minutes."""
        pe = PersistenceEngine()
        old_ts    = datetime.now(timezone.utc) - timedelta(minutes=90)
        recent_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        pe._observations[MAC_A] = [
            {**_obs(0), "timestamp": old_ts},
            {**_obs(0), "timestamp": recent_ts},
        ]
        pe.purge_old_observations(max_age_minutes=60)
        remaining = pe._observations.get(MAC_A, [])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["timestamp"], recent_ts)

    def test_purge_removes_mac_with_no_remaining_observations(self):
        """purge_old_observations() removes a MAC entirely when all its data is stale."""
        pe = PersistenceEngine()
        pe._observations[MAC_A] = [{**_obs(0), "timestamp": datetime.now(timezone.utc) - timedelta(hours=2)}]
        pe.purge_old_observations(max_age_minutes=60)
        self.assertNotIn(MAC_A, pe._observations)


class TestGetSuspiciousDevices(unittest.TestCase):

    def test_get_suspicious_devices_filters_by_threshold(self):
        """get_suspicious_devices() returns only devices above threshold."""
        pe = PersistenceEngine(poll_interval_seconds=240, alert_threshold=0.7)
        # MAC_A: 4 observations across all windows → temporal=1.0, freq≈0.8
        pe._observations[MAC_A] = [_obs(m) for m in [2, 7, 12, 17]]
        # MAC_B: only 1 observation → score = 0.0
        pe._observations[MAC_B] = [_obs(1)]

        suspicious = pe.get_suspicious_devices(threshold=0.3)
        macs = [d["mac"] for d in suspicious]
        self.assertIn(MAC_A, macs)
        self.assertNotIn(MAC_B, macs)


class TestAlertLevel(unittest.TestCase):

    def test_alert_level_assignments(self):
        """_make_alert_level() returns correct label for each score range."""
        pe = PersistenceEngine()
        self.assertEqual(pe._make_alert_level(0.95), "high")
        self.assertEqual(pe._make_alert_level(0.90), "high")
        self.assertEqual(pe._make_alert_level(0.89), "likely")
        self.assertEqual(pe._make_alert_level(0.70), "likely")
        self.assertEqual(pe._make_alert_level(0.69), "suspicious")
        self.assertEqual(pe._make_alert_level(0.50), "suspicious")
        self.assertEqual(pe._make_alert_level(0.10), "suspicious")

    def test_detection_event_has_alert_level(self):
        """DetectionEvent returned by update() carries an alert_level string."""
        pe = PersistenceEngine(alert_threshold=0.3, poll_interval_seconds=240)
        pe._observations[MAC_A] = [_obs(m) for m in [2, 7, 12, 17]]
        devices = [{"macaddr": MAC_A, "manuf": "", "type": "", "name": "", "last_signal": None}]
        events = pe.update(devices, gps_fix=None)
        self.assertTrue(len(events) > 0)
        self.assertIn(events[0].alert_level, ("suspicious", "likely", "high"))


class TestStats(unittest.TestCase):

    def test_stats_returns_correct_structure(self):
        """stats() returns a dict with all required keys."""
        pe = PersistenceEngine()
        s = pe.stats()
        for key in ("total_devices_tracked", "suspicious_count",
                    "oldest_observation", "newest_observation"):
            self.assertIn(key, s)

    def test_stats_counts_are_correct(self):
        """stats() accurately reflects tracked device counts."""
        pe = PersistenceEngine(poll_interval_seconds=240, alert_threshold=0.3)
        # MAC_A: above threshold (many obs); MAC_B: single obs → score 0.0
        pe._observations[MAC_A] = [_obs(m) for m in [2, 7, 12, 17]]
        pe._observations[MAC_B] = [_obs(1)]
        s = pe.stats()
        self.assertEqual(s["total_devices_tracked"], 2)
        self.assertIsNotNone(s["oldest_observation"])
        self.assertIsNotNone(s["newest_observation"])


if __name__ == "__main__":
    unittest.main()
