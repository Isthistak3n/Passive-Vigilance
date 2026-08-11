"""Tests for modules/alert_policy.py — the central page/suppress decision.

Uses the real RateLimiter (in-memory, no persistence) so the verdicts reflect
the actual cooldown behaviour the orchestrator sees, including the #241
proximity-bucket flood containment the policy composes.
"""

from datetime import datetime, timezone

import pytest

from modules import alert_policy
from modules.alerts import RateLimiter
from modules.persistence import DetectionEvent


def _make_event(**overrides) -> DetectionEvent:
    defaults = dict(
        mac="aa:bb:cc:dd:ee:ff",
        score=0.85,
        score_breakdown={"novelty": 0.85},
        first_seen=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 1, 12, 20, 0, tzinfo=timezone.utc),
        locations=[],
        observation_count=10,
        manufacturer="Apple",
        device_type="phone",
        alert_level="likely",
        fingerprint="mac:aa:bb:cc:dd:ee:ff",
    )
    defaults.update(overrides)
    return DetectionEvent(**defaults)


@pytest.fixture()
def policy():
    return alert_policy.AlertPolicy(
        RateLimiter(cooldown_seconds=300),
        wifi_page_min_score=0.7,
        egregious_cooldown_seconds=3600,
        wids_cooldown_seconds=600,
    )


# ---------------------------------------------------------------------------
# WiFi persistence — threshold gate
# ---------------------------------------------------------------------------


class TestWifiThresholdGate:
    @pytest.mark.asyncio
    async def test_below_threshold_does_not_page(self, policy):
        verdict = await policy.wifi_persistence(
            _make_event(score=0.5, alert_level="suspicious"), signal=-60)
        assert verdict.page is False
        assert verdict.reason == alert_policy.BELOW_THRESHOLD

    @pytest.mark.asyncio
    async def test_below_threshold_never_touches_the_limiter(self, policy):
        """A display-only event must not start a cooldown it never paged for."""
        low = _make_event(score=0.5, alert_level="suspicious")
        high = _make_event(score=0.9, alert_level="high")
        await policy.wifi_persistence(low, signal=-60)
        verdict = await policy.wifi_persistence(high, signal=-60)
        assert verdict.page is True

    @pytest.mark.asyncio
    async def test_above_threshold_pages(self, policy):
        verdict = await policy.wifi_persistence(_make_event(score=0.9), signal=-60)
        assert verdict.page is True
        assert verdict.reason == alert_policy.PAGE
        assert verdict.key == "persist:mac:aa:bb:cc:dd:ee:ff"

    @pytest.mark.asyncio
    async def test_repeat_within_cooldown_is_suppressed(self, policy):
        event = _make_event(score=0.9)
        assert (await policy.wifi_persistence(event, signal=-60)).page is True
        verdict = await policy.wifi_persistence(event, signal=-60)
        assert verdict.page is False
        assert verdict.reason == alert_policy.COOLDOWN

    @pytest.mark.asyncio
    async def test_force_page_overrides_the_threshold(self, policy):
        """Egregious-during-baseline (design 5.2) pages at score 0.5."""
        event = _make_event(score=0.5, alert_level="suspicious", force_page=True)
        verdict = await policy.wifi_persistence(event, signal=-35)
        assert verdict.page is True


# ---------------------------------------------------------------------------
# WiFi persistence — cooldown keying (composes alert_suppression)
# ---------------------------------------------------------------------------


class TestWifiCooldownKeys:
    @pytest.mark.asyncio
    async def test_fingerprinted_device_keys_on_its_fingerprint(self, policy):
        event = _make_event(
            mac="aa:bb:cc:dd:ee:01", mac_type="randomized",
            fingerprint="wifi-fp:1234", score=0.9)
        verdict = await policy.wifi_persistence(event, signal=-60)
        assert verdict.key == "persist:wifi-fp:1234"

    @pytest.mark.asyncio
    async def test_rotations_of_one_contact_share_one_cooldown(self, policy):
        """#193: the fingerprint key collapses MAC rotations into one bucket."""
        first = _make_event(
            mac="aa:bb:cc:dd:ee:01", mac_type="randomized",
            fingerprint="wifi-fp:1234", score=0.9)
        rotated = _make_event(
            mac="aa:bb:cc:dd:ee:02", mac_type="randomized",
            fingerprint="wifi-fp:1234", score=0.9)
        assert (await policy.wifi_persistence(first, signal=-60)).page is True
        assert (await policy.wifi_persistence(rotated, signal=-60)).page is False

    @pytest.mark.asyncio
    async def test_unfingerprintable_rotator_collapses_to_proximity_bucket(self, policy):
        """#241: a randomized device on a mac: key shares the coarse band bucket."""
        event = _make_event(
            mac="da:bb:cc:dd:ee:01", mac_type="randomized",
            fingerprint="mac:da:bb:cc:dd:ee:01", score=0.5, force_page=True)
        verdict = await policy.wifi_persistence(event, signal=-35)
        assert verdict.key == "proximity:wifi:vclose"


# ---------------------------------------------------------------------------
# The dense-LEARNING flood replay
# ---------------------------------------------------------------------------


class TestDenseLearningFloodReplay:
    @pytest.mark.asyncio
    async def test_force_page_flood_collapses_to_the_bucket_ceiling(self, policy):
        """The 1.0-gate scenario: a dense node in its learning window sees many
        close, un-fingerprintable randomized devices, each rotating its MAC and
        re-flagged as egregious every poll. Pages must be bounded by the tiny
        proximity-bucket space, not the device/rotation count."""
        pages = 0
        for i in range(150):
            event = _make_event(
                mac=f"da:bb:cc:dd:{i // 256:02x}:{i % 256:02x}",
                mac_type="randomized",
                fingerprint=f"mac:da:bb:cc:dd:{i // 256:02x}:{i % 256:02x}",
                score=0.5, alert_level="suspicious", force_page=True)
            # Signals spread across the close and far bands.
            signal = -45 if i % 2 else -70
            verdict = await policy.wifi_persistence(event, signal=signal)
            pages += 1 if verdict.page else 0
        # One page per occupied proximity band (close + far), not 150.
        assert pages == 2

    @pytest.mark.asyncio
    async def test_a_new_in_room_egregious_device_still_pages_through_the_flood(self, policy):
        """The far/close flood must not mask the first VERY CLOSE contact — it
        lands in its own band bucket and pages immediately."""
        for i in range(50):
            event = _make_event(
                mac=f"da:bb:cc:dd:00:{i:02x}", mac_type="randomized",
                fingerprint=f"mac:da:bb:cc:dd:00:{i:02x}",
                score=0.5, force_page=True)
            await policy.wifi_persistence(event, signal=-70)
        in_room = _make_event(
            mac="da:bb:cc:dd:ff:01", mac_type="randomized",
            fingerprint="mac:da:bb:cc:dd:ff:01", score=0.5, force_page=True)
        verdict = await policy.wifi_persistence(in_room, signal=-30)
        assert verdict.page is True
        assert verdict.key == "proximity:wifi:vclose"

    @pytest.mark.asyncio
    async def test_a_fingerprinted_contact_is_not_masked_by_the_flood(self, policy):
        """Rotation-stable identities keep their per-contact key — the bucket
        cooldown of the un-trackable flood never suppresses them."""
        for i in range(50):
            event = _make_event(
                mac=f"da:bb:cc:dd:00:{i:02x}", mac_type="randomized",
                fingerprint=f"mac:da:bb:cc:dd:00:{i:02x}",
                score=0.5, force_page=True)
            await policy.wifi_persistence(event, signal=-45)
        tracked = _make_event(
            mac="da:bb:cc:dd:ff:02", mac_type="randomized",
            fingerprint="wifi-fp:distinct", score=0.9)
        verdict = await policy.wifi_persistence(tracked, signal=-45)
        assert verdict.page is True


# ---------------------------------------------------------------------------
# Aircraft
# ---------------------------------------------------------------------------


class TestAircraft:
    @pytest.mark.asyncio
    async def test_transit_is_display_only(self, policy):
        verdict = await policy.aircraft("ABC123", emergency=False, of_interest=False)
        assert verdict.page is False
        assert verdict.reason == alert_policy.DISPLAY_ONLY
        assert verdict.key is None

    @pytest.mark.asyncio
    async def test_of_interest_pages_on_its_icao_key(self, policy):
        verdict = await policy.aircraft("ABC123", emergency=False, of_interest=True)
        assert verdict.page is True
        assert verdict.key == "aircraft:ABC123"

    @pytest.mark.asyncio
    async def test_emergency_keys_separately_so_it_is_never_masked(self, policy):
        """A routine of-interest page must not suppress the first emergency."""
        await policy.aircraft("ABC123", emergency=False, of_interest=True)
        verdict = await policy.aircraft("ABC123", emergency=True, of_interest=True)
        assert verdict.page is True
        assert verdict.key == "aircraft-emergency:ABC123"

    @pytest.mark.asyncio
    async def test_held_emergency_does_not_repage_every_poll(self, policy):
        assert (await policy.aircraft("ABC123", emergency=True, of_interest=False)).page
        verdict = await policy.aircraft("ABC123", emergency=True, of_interest=False)
        assert verdict.page is False
        assert verdict.reason == alert_policy.COOLDOWN


# ---------------------------------------------------------------------------
# Drone RF and Remote ID
# ---------------------------------------------------------------------------


class TestDroneAndRemoteId:
    @pytest.mark.asyncio
    async def test_drone_pages_once_per_band_per_window(self, policy):
        assert (await policy.drone(2400.0)).page is True
        assert (await policy.drone(2400.0)).page is False
        assert (await policy.drone(5800.0)).page is True

    @pytest.mark.asyncio
    async def test_drone_key_matches_the_legacy_format(self, policy):
        verdict = await policy.drone(2400.0)
        assert verdict.key == "drone:2400mhz"

    @pytest.mark.asyncio
    async def test_remote_id_pages_once_per_uas_per_window(self, policy):
        assert (await policy.remote_id("UAS-1")).page is True
        assert (await policy.remote_id("UAS-1")).page is False
        assert (await policy.remote_id("UAS-2")).page is True


# ---------------------------------------------------------------------------
# Kismet WIDS
# ---------------------------------------------------------------------------


class TestWids:
    @pytest.mark.asyncio
    async def test_pages_once_per_header_per_transmitter(self, policy):
        first = await policy.wids("CRYPTODROP", "AA:BB:CC:DD:EE:FF")
        repeat = await policy.wids("CRYPTODROP", "AA:BB:CC:DD:EE:FF")
        assert first.page is True
        assert first.key == "wids:CRYPTODROP:AA:BB:CC:DD:EE:FF"
        assert repeat.page is False

    @pytest.mark.asyncio
    async def test_a_different_ap_raising_the_same_alert_still_pages(self, policy):
        await policy.wids("CRYPTODROP", "AA:BB:CC:DD:EE:FF")
        verdict = await policy.wids("CRYPTODROP", "11:22:33:44:55:66")
        assert verdict.page is True

    @pytest.mark.asyncio
    async def test_different_alert_types_from_one_ap_page_independently(self, policy):
        await policy.wids("CRYPTODROP", "AA:BB:CC:DD:EE:FF")
        verdict = await policy.wids("BSSTIMESTAMP", "AA:BB:CC:DD:EE:FF")
        assert verdict.page is True
