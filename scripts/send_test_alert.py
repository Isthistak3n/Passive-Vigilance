#!/usr/bin/env python3
"""Validate an alert backend end to end by firing a test page through it.

The last 1.0 gate (#191) requires one real alert backend validated end to end.
Waiting for a genuine detection to find out whether credentials work is the
wrong feedback loop — this tool sends a synthetic page through the exact code
path the orchestrator uses (AlertFactory -> backend.send) and reports what
happened, so a backend can be proven working the moment its .env slots are
filled, and re-proven after any credential change.

Usage
-----
  # Validate whatever ALERT_BACKEND in .env selects
  python3 scripts/send_test_alert.py

  # Validate a specific backend regardless of .env
  python3 scripts/send_test_alert.py --backend telegram

  # Also exercise every typed formatter (persistence/aircraft/drone/remote-id)
  python3 scripts/send_test_alert.py --full

Exit codes: 0 = sent OK; 1 = backend reachable but send failed; 2 = the
requested backend is not configured (fell back to console).
"""

import argparse
import os
import socket
import sys
from datetime import datetime, timezone

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

from modules.alerts import AlertFactory, ConsoleBackend  # noqa: E402
from modules.persistence import DetectionEvent  # noqa: E402


def _test_event() -> DetectionEvent:
    """A clearly-labeled synthetic persistence event for --full."""
    now = datetime.now(timezone.utc)
    return DetectionEvent(
        mac="00:00:5e:00:53:01",  # IANA documentation range — never a real device
        score=0.99,
        score_breakdown={"test": 0.99},
        first_seen=now, last_seen=now,
        locations=[],
        observation_count=1,
        manufacturer="TEST",
        device_type="TEST DEVICE (synthetic)",
        alert_level="high",
        fingerprint="mac:00:00:5e:00:53:01",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a test alert through the configured backend")
    parser.add_argument(
        "--backend", default=None,
        help="Backend to test (ntfy/telegram/discord/console); default = ALERT_BACKEND from .env")
    parser.add_argument(
        "--full", action="store_true",
        help="Also exercise the typed alert formatters, not just the generic send")
    args = parser.parse_args(argv)

    backend = AlertFactory.get_backend(backend_name=args.backend)
    name = type(backend).__name__
    requested = (args.backend or "").strip().lower() or None
    print(f"Backend resolved: {name}")

    # AlertFactory silently degrades to console when the requested backend is
    # missing credentials — for a validation tool that degradation IS the
    # failure, so detect and report it instead of "passing" on console.
    if requested and requested != "console" and isinstance(backend, ConsoleBackend):
        print(f"FAIL: backend '{requested}' is not configured (fell back to console).")
        print("Fill its .env slots (see .env.example) and re-run.")
        return 2

    host = socket.gethostname()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ok = backend.send(
        "Passive Vigilance — test alert",
        f"End-to-end backend validation from {host} at {stamp}. "
        "If you can read this, the paging path works.",
        priority="high",
        tags=["white_check_mark", "test"],
    )
    print(f"Generic send: {'OK' if ok else 'FAILED'}")

    if args.full and ok:
        results = {
            "persistence": backend.send_persistence_alert(_test_event()),
            "aircraft": backend.send_aircraft_alert({
                "icao": "TEST00", "callsign": "TEST",
                "registration": "N-TEST", "operator": "TEST",
                "country": "N/A", "altitude": 0, "emergency": False,
            }),
            "drone": backend.send_drone_alert({
                "freq_mhz": 0.0, "power_db": 0.0, "lat": 0.0, "lon": 0.0,
            }),
            "remote_id": backend.send_remote_id_alert({
                "uas_id": "TEST-UAS", "ua_type": "TEST", "status": "TEST",
            }),
        }
        for path, sent in results.items():
            # A False here can be the backend's own rate limiter, not a failure
            # — re-running --full within the cooldown windows suppresses repeats.
            print(f"Typed {path}: {'OK' if sent else 'suppressed/failed'}")

    if not ok:
        print("Send failed — check the WARNING lines above for the backend's error.")
        return 1
    print("Backend validated. This same path is what pages real detections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
