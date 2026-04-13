"""Passive Vigilance — main entry point."""

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from modules.alerts import NtfyBackend
from modules.dump1090 import ADSBModule
from modules.drone_rf import DroneRFModule
from modules.gps import GPSModule
from modules.kismet import KismetModule
from modules.shapefile import ShapefileWriter
from modules.wigle import WiGLEUploader

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run(stop_event: asyncio.Event) -> None:
    """Main sensor loop — initialises all modules and processes events until stopped."""
    gps = GPSModule()
    kismet = KismetModule()
    adsb = ADSBModule()
    drone_rf = DroneRFModule()
    shapefile = ShapefileWriter()
    wigle = WiGLEUploader()

    alert_backend_name = os.getenv("ALERT_BACKEND", "ntfy")
    if alert_backend_name == "ntfy":
        alerts = NtfyBackend(
            server=os.getenv("NTFY_SERVER", "https://ntfy.sh"),
            topic=os.getenv("NTFY_TOPIC", "passive-vigilance"),
        )
    else:
        raise ValueError(f"Unsupported ALERT_BACKEND: {alert_backend_name!r}")

    logger.info("Starting Passive Vigilance sensor platform")

    # TODO: connect modules and start background tasks
    # gps.connect()
    # kismet.connect()
    # adsb.connect()
    # drone_rf.start_scan()

    try:
        while not stop_event.is_set():
            # TODO: poll sensors, detect events, write shapefile records, fire alerts
            await asyncio.sleep(1)
    finally:
        logger.info("Shutting down — releasing resources")
        # TODO: close modules
        # drone_rf.stop_scan()
        # adsb.close()
        # kismet.close()
        # gps.close()


def main() -> None:
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(run(stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
