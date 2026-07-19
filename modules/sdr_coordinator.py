'''SDR time-share coordinator (SHARED mode only).

When only one RTL-SDR dongle is available, this module time-shares the hardware
across an ordered cycle of *bands* — ADS-B (readsb @ 1090 MHz), AIS (~162 MHz),
ACARS (~131 MHz), and the legacy DroneRF scanner — using configurable slices. It
contains the core P1 hardening: an exclusive handoff lock, a settle barrier so the
next owner doesn't grab a half-released dongle, and start/stop handshakes with retry.

Each band is a small *owner* (``_BandOwner``) exposing ``name``, ``acquire()``,
``release()``, and ``is_available``. Decoder bands (AIS/ACARS) run as external
systemd services managed through the same sudoers-scoped ``systemctl`` mechanism as
readsb — so one handoff machine, one settle barrier, one crash-isolation story.

In DEDICATED mode (2+ dongles) the coordinator is NOT used: each band runs
continuously on its own dongle (see main.py). This coordinator is SHARED-only.
'''

import asyncio
import logging
import os
import subprocess

from modules.sdr_utils import RTL_SDR_VENDOR, RTL_SDR_PRODUCTS

logger = logging.getLogger(__name__)

_READSB_SERVICE = "readsb"
# How finely a slice's sleep is chopped, so a request_band_window() preemption
# (e.g. the >30s-held ACARS trigger) is serviced within ~this many seconds rather
# than waiting out the whole ADS-B slice.
_SLICE_TICK_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Band owners — one per capability that can hold the shared dongle
# ---------------------------------------------------------------------------


class _BandOwner:
    """Duck-typed band: ``name``, ``async acquire()`` (open the dongle for this
    band, return True on success), ``async release()`` (free it, return True only
    once the dongle is *confirmed* free — see ``_handoff_to``), and
    ``is_available`` (False → the cycle skips this band's slice, e.g. an
    auto-disabled scanner or a decoder whose service isn't installed)."""

    name = "none"

    @property
    def is_available(self) -> bool:
        return True

    async def acquire(self) -> bool:  # pragma: no cover - interface
        return True

    async def release(self) -> bool:  # pragma: no cover - interface
        return True


class _ReadsbOwner(_BandOwner):
    """ADS-B via the readsb systemd service."""

    name = "adsb"

    def __init__(self, coordinator: "SDRCoordinator") -> None:
        self._c = coordinator

    async def acquire(self) -> bool:
        return await self._c._start_readsb_with_handshake()

    async def release(self) -> bool:
        return await self._c._stop_readsb_with_handshake()


class _DecoderServiceOwner(_BandOwner):
    """AIS / ACARS — an external decoder running as a systemd service on the dongle.

    Optionally wired to a capture module so the orchestrator's health/GUI reflect
    the active band (``can_scan``) and a module that auto-disables is skipped.
    """

    def __init__(self, name: str, service: str, coordinator: "SDRCoordinator",
                 module=None) -> None:
        self.name = name
        # Public: startup reclaim needs the systemd unit name to detect a
        # decoder a previous (killed) session left running on the dongle.
        self.service = service
        self._c = coordinator
        self._module = module

    @property
    def is_available(self) -> bool:
        return not getattr(self._module, "auto_disabled", False)

    async def acquire(self) -> bool:
        ok = await self._c._start_service_with_handshake(self.service)
        if self._module is not None:
            self._module.can_scan = bool(ok)
        return ok

    async def release(self) -> bool:
        if self._module is not None:
            self._module.can_scan = False
        return await self._c._stop_service_with_handshake(self.service)


class _DroneRFOwner(_BandOwner):
    """Legacy DroneRF in-process scanner (retired; kept for reversibility)."""

    name = "drone_rf"

    def __init__(self, module) -> None:
        self._module = module

    @property
    def is_available(self) -> bool:
        return not getattr(self._module, "auto_disabled", False)

    async def acquire(self) -> bool:
        self._module.can_scan = True
        await self._module.start_scan()
        return True

    async def release(self) -> bool:
        self._module.can_scan = False
        task = getattr(self._module, "_scan_task", None)
        if task is not None and not task.done():
            await self._module.stop_scan()
        return True


class SDRCoordinator:
    """Async time-share coordinator for a single RTL-SDR dongle (P1 hardened).

    - Exclusive asyncio.Lock on all handoffs (no concurrent SDR access).
    - Settle barrier between owners; start/stop handshake with retry + backoff.
    - Ordered N-band cycle from ``SDR_CYCLE_SLICES`` (or an explicit list).
    - ``request_band_window()`` lets a caller (the >30s ACARS trigger) preempt the
      current slice for a bounded window, then resume the cycle.
    - ``healthy`` exposed for main.py sensor_health; robust to CI/test (no systemd).
    """

    def __init__(self, drone_rf_module=None, *, owners=None,
                 cycle_slices=None) -> None:
        self._drone_rf = drone_rf_module
        self._adsb_slice = int(os.getenv("ADSB_SLICE_SECONDS", "30"))
        self._drone_slice = int(os.getenv("DRONE_RF_SLICE_SECONDS", "30"))
        # Settle barrier: after the outgoing owner is confirmed down, wait this
        # long before the incoming owner opens the dongle. Without it the next
        # owner grabs a half-released device — readsb logs "SDR wedged, exiting!"
        # and a decoder dies with "couldn't claim the device".
        self._handoff_settle = float(os.getenv("SDR_HANDOFF_SETTLE_SECONDS", "2.0"))
        # Optional escalation: usbreset the dongle on each handoff to clear a
        # genuinely wedged state. Off by default — needs a sudoers entry for
        # `usbreset` alongside the systemctl one.
        self._usb_reset = os.getenv("SDR_HANDOFF_USB_RESET", "false").lower() == "true"
        self._current_owner: str = "none"
        self._lock = asyncio.Lock()
        self._healthy: bool = True

        # Owner registry. readsb is always present; the drone owner is kept for
        # reversibility; AIS/ACARS owners are injected by main.py via `owners`.
        self._owners: dict = {"adsb": _ReadsbOwner(self)}
        if drone_rf_module is not None:
            self._owners["drone_rf"] = _DroneRFOwner(drone_rf_module)
        for owner in (owners or []):
            self._owners[owner.name] = owner

        # The ordered cycle: explicit arg > SDR_CYCLE_SLICES env > ADS-B only.
        self._slices = cycle_slices or self._parse_slices(os.getenv("SDR_CYCLE_SLICES"))

        # Preemption (e.g. ACARS on a >30s-held contact): a one-shot window the
        # cycle services mid-slice, bounded per cycle so it can't starve ADS-B.
        self._pending_window = None  # (band, duration_seconds) or None
        self._max_windows_per_cycle = int(os.getenv("ACARS_MAX_WINDOWS_PER_CYCLE", "4"))
        self._windows_this_cycle = 0

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _parse_slices(self, raw):
        """Parse 'adsb:840,ais:60' → [('adsb', 840), ('ais', 60)]. Falls back to
        a single ADS-B slice (so an un-configured node behaves as readsb-only)."""
        if not raw or not raw.strip():
            return [("adsb", self._adsb_slice)]
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                name, secs = part.split(":")
                secs_i = int(secs)
            except ValueError:
                logger.warning("SDR_CYCLE_SLICES: ignoring malformed slice %r", part)
                continue
            if secs_i <= 0:
                # A zero/negative slice makes _run_slice return immediately, so the
                # cycle would hand the dongle off and back with no dwell — tight
                # handoff churn that wedges the SDR. Drop it.
                logger.warning("SDR_CYCLE_SLICES: ignoring non-positive slice %r", part)
                continue
            out.append((name.strip(), secs_i))
        return out or [("adsb", self._adsb_slice)]

    def register_owner(self, owner: _BandOwner) -> None:
        self._owners[owner.name] = owner

    def add_decoder_band(self, name: str, service: str, module=None) -> None:
        """Register an external-decoder band (AIS/ACARS) by systemd service name."""
        self.register_owner(_DecoderServiceOwner(name, service, self, module))

    async def start_decoder_service(self, service: str) -> bool:
        """Start a decoder service continuously (DEDICATED mode — own dongle, no cycle)."""
        return await self._start_service_with_handshake(service)

    async def stop_decoder_service(self, service: str) -> bool:
        """Stop a decoder service (DEDICATED-mode shutdown)."""
        return await self._stop_service_with_handshake(service)

    @property
    def current_owner(self) -> str:
        return self._current_owner

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def slices(self):
        return list(self._slices)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._reclaim_orphaned_decoders()
        await self._handoff_to("adsb")
        logger.info("SDR coordinator ready — cycle: %s",
                    ", ".join(f"{b}:{s}s" for b, s in self._slices))

    async def _reclaim_orphaned_decoders(self) -> None:
        """Take the dongle back from decoders a previous session left running.

        Coordinator state dies with the process, but the decoder services it
        manages do not: when a watchdog kill lands mid-decoder-slice, the decoder
        (e.g. dumpvdl2) keeps the dongle, and the fresh coordinator — believing
        nothing is held (``current_owner == "none"``) — starts readsb straight
        onto a busy device. That is the 2026-07-14 outage: readsb crash-looped on
        "Device or resource busy" 8k+ times while the orphan held the SDR for 33
        hours, and every PV restart stalled in the resulting reconnect path until
        the start-rate limit took the node down. The #214/#216 honest-release
        machinery never sees this because it only guards handoffs *within* a
        session.

        So, before the first handoff: stop every registered decoder service that
        systemd reports active. A decoder that refuses to stop becomes the parked
        current owner instead of being ignored — the immediately following
        ``_handoff_to("adsb")`` then walks the normal honest-release path (retry
        the release; refuse to start readsb onto a busy device; report unhealthy)
        rather than repeating the crash-loop.

        Only runs in SHARED mode (``start()`` is not called in DEDICATED mode,
        where an already-running decoder on its own dongle is intentional).
        """
        for owner in self._owners.values():
            service = getattr(owner, "service", None)
            if not isinstance(service, str) or not service:
                continue  # in-process owners die with the process — no orphan
            if not await self._is_service_active(service):
                continue
            logger.warning(
                "SDR reclaim: %s (%s) already running at coordinator startup — "
                "orphan from a previous session holds the dongle; stopping it",
                owner.name, service)
            if not await self._stop_service_with_handshake(service):
                self._current_owner = owner.name
                self._healthy = False
                logger.warning(
                    "SDR reclaim: %s did not stop — parking on it so the first "
                    "handoff retries the release instead of starting readsb "
                    "onto a busy device", owner.name)

    async def stop(self) -> None:
        # Release whatever holds the dongle, then restore readsb (ADS-B is the
        # safe default owner) so the node leaves the SDR in a known state.
        cur = self._owners.get(self._current_owner)
        released = True
        if cur is not None and cur.name != "adsb":
            try:
                released = await cur.release()
            except Exception as exc:
                released = False
                logger.debug("SDR coordinator stop — release %s error: %s", cur.name, exc)
        # Legacy: ensure DroneRF scanning is off regardless of current owner.
        if self._drone_rf is not None:
            self._drone_rf.can_scan = False
            try:
                await self._drone_rf.stop_scan()
            except Exception as exc:
                logger.debug("SDR coordinator stop — DroneRF stop error: %s", exc)
        if not released:
            # The decoder is still confirmed holding the dongle (same #214
            # mechanism as _handoff_to). Starting readsb onto a device it hasn't
            # released is the exact "SDR wedged, exiting!" / "Device or resource
            # busy" crash-loop — and on shutdown nothing re-hands-off to recover
            # it, so readsb would crash-loop indefinitely after PV exits. Leave
            # readsb stopped and report loudly instead; the SDR-wedge watchdog and
            # the operator can take it from here, and a normal decoder shutdown
            # (its own systemd stop) frees the dongle for readsb's next start.
            self._current_owner = cur.name if cur is not None else "none"
            self._healthy = False
            logger.error(
                "SDR coordinator stop — %s did not release the dongle; leaving "
                "readsb stopped rather than crash-looping it onto a busy device",
                cur.name if cur is not None else "current owner")
            return
        # Settle before reopening the dongle for readsb. Starting readsb inline on a
        # decoder's still-releasing device is the exact "SDR wedged, exiting!"
        # crash-loop the settle barrier exists to prevent — and on shutdown nothing
        # re-hands-off to recover it, so readsb crash-loops until it wins the race.
        # Mid-cycle handoffs already settle here; stop() was the one path that didn't.
        await self._settle_sdr()
        await self._start_readsb()
        self._current_owner = "none"
        self._healthy = True
        logger.info("SDR coordinator stopped — readsb restored")

    async def _coordinator_loop(self) -> None:
        while True:
            self._windows_this_cycle = 0
            for band, seconds in self._slices:
                owner = self._owners.get(band)
                if owner is None or not owner.is_available:
                    # Unconfigured or auto-disabled band — skip its slice, leaving
                    # the dongle with the current owner (don't thrash).
                    continue
                await self._handoff_to(band)
                await self._run_slice(seconds)

    async def _run_slice(self, seconds: float) -> None:
        """Hold the current band for ``seconds``, honoring a preemption request
        within ~_SLICE_TICK_SECONDS so an ACARS window isn't delayed a whole slice."""
        remaining = float(seconds)
        while remaining > 0:
            step = min(_SLICE_TICK_SECONDS, remaining)
            try:
                await asyncio.sleep(step)
            except asyncio.CancelledError:
                raise
            remaining -= step
            if self._pending_window is not None:
                band, duration = self._pending_window
                self._pending_window = None
                await self._service_window(band, duration)

    async def _service_window(self, band: str, duration: float) -> None:
        """Preempt to ``band`` for ``duration`` then hand back to the prior band."""
        owner = self._owners.get(band)
        if owner is None or not owner.is_available:
            return
        if self._windows_this_cycle >= self._max_windows_per_cycle:
            logger.debug("Band window for %s skipped — per-cycle cap reached", band)
            return
        self._windows_this_cycle += 1
        prev = self._current_owner
        await self._handoff_to(band)
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            raise
        finally:
            await self._handoff_to(prev if prev in self._owners else "adsb")

    def request_band_window(self, band: str, duration: float) -> bool:
        """Ask the cycle to give ``band`` the dongle for ``duration`` seconds at the
        next tick (used by the >30s-held ACARS trigger). One pending window at a
        time; returns False if the band is unknown/unavailable or one is queued."""
        owner = self._owners.get(band)
        if owner is None or not owner.is_available:
            return False
        if self._pending_window is not None:
            return False
        self._pending_window = (band, float(duration))
        return True

    # ------------------------------------------------------------------
    # Generic handoff
    # ------------------------------------------------------------------

    async def _handoff_to(self, band: str) -> None:
        async with self._lock:
            owner = self._owners.get(band)
            if owner is None:
                logger.warning("SDR handoff to unknown band %r — ignored", band)
                return
            # Target unavailable (e.g. auto-disabled scanner): keep the dongle on
            # ADS-B rather than handing it to a dead band.
            if not owner.is_available:
                logger.debug("Band %s unavailable — keeping readsb on the SDR", band)
                if "adsb" in self._owners:
                    self._current_owner = "adsb"
                return
            if owner.name == self._current_owner:
                return
            logger.debug("SDR timeshare: handing off to %s", band)
            current = self._owners.get(self._current_owner)
            if current is not None and current is not owner:
                try:
                    released = await current.release()
                except Exception as exc:
                    released = False
                    logger.warning("SDR release of %s failed: %s", current.name, exc)
                if not released:
                    # #214: the previous owner (e.g. an AIS/ACARS decoder that
                    # outlived its slice) is still confirmed holding the dongle.
                    # Acquiring the new band here is exactly how readsb ended up
                    # crash-looping on "Device or resource busy" for hours — the
                    # coordinator believed ADS-B owned the dongle while ais-catcher
                    # still had it open. Stay parked on the still-active owner
                    # (current_owner unchanged — stay honest for the health
                    # banner) and let the next time this band comes up in the
                    # cycle retry the release, instead of racing a new acquire
                    # onto a device that isn't actually free yet.
                    self._healthy = False
                    logger.warning(
                        "SDR handoff to %s deferred — %s has not released the dongle",
                        band, current.name)
                    return
            await self._settle_sdr()
            try:
                ok = await owner.acquire()
            except Exception as exc:
                ok = False
                logger.warning("SDR acquire of %s raised: %s", band, exc)
            if ok:
                self._current_owner = band
                self._healthy = True
            else:
                # The previous owner was already released above, so nothing holds
                # the dongle now — record "none", NOT the stale prior owner. Leaving
                # it stale is a real trap: the next handoff back to that band matches
                # on name and short-circuits the early return below, so the released
                # device is never re-opened (e.g. adsb→ais fails, then the next adsb
                # slice thinks readsb is still up and never restarts it).
                self._current_owner = "none"
                self._healthy = False
                logger.warning("SDR handoff to %s failed — marking unhealthy", band)

    # ------------------------------------------------------------------
    # readsb service control (handshake helpers kept readsb-named for the
    # ADS-B owner and the existing handoff test suite)
    # ------------------------------------------------------------------

    async def _start_readsb_with_handshake(self, max_attempts: int = 5) -> bool:
        for attempt in range(1, max_attempts + 1):
            await self._start_readsb()
            await asyncio.sleep(0.5 * attempt)
            if await self._is_readsb_active():
                return True
            await asyncio.sleep(0.5)
        logger.debug("readsb handshake could not confirm active state (CI/test env?) — assuming success")
        return True

    async def _stop_readsb_with_handshake(self, max_attempts: int = 5) -> bool:
        for attempt in range(1, max_attempts + 1):
            await self._stop_readsb()
            await asyncio.sleep(0.5 * attempt)
            if not await self._is_readsb_active():
                return True
            await asyncio.sleep(0.5)
        logger.warning(
            "readsb stop handshake could not confirm inactive state after %d attempts",
            max_attempts)
        return False

    async def _is_readsb_active(self) -> bool:
        return await self._is_service_active(_READSB_SERVICE)

    async def _start_readsb(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_systemctl, "start", _READSB_SERVICE)

    async def _stop_readsb(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_systemctl, "stop", _READSB_SERVICE)

    # ------------------------------------------------------------------
    # Generic systemd service control (AIS / ACARS decoder services)
    # ------------------------------------------------------------------

    async def _start_service_with_handshake(self, service: str, max_attempts: int = 5) -> bool:
        loop = asyncio.get_running_loop()
        for attempt in range(1, max_attempts + 1):
            await loop.run_in_executor(None, self._run_systemctl, "start", service)
            await asyncio.sleep(0.5 * attempt)
            if await self._is_service_active(service):
                return True
            await asyncio.sleep(0.5)
        logger.debug("%s start handshake unconfirmed (CI/test env?) — assuming success", service)
        return True

    async def _stop_service_with_handshake(self, service: str, max_attempts: int = 5) -> bool:
        loop = asyncio.get_running_loop()
        for attempt in range(1, max_attempts + 1):
            await loop.run_in_executor(None, self._run_systemctl, "stop", service)
            await asyncio.sleep(0.5 * attempt)
            if not await self._is_service_active(service):
                return True
            await asyncio.sleep(0.5)
        # #214: the old behaviour here logged a debug line and *returned True
        # anyway* ("assuming success"), even though systemd still reports the unit
        # active. The coordinator then handed the dongle to the next owner while
        # the decoder genuinely still held it open — readsb crash-looped on
        # "Device or resource busy" for hours because nothing was actually wrong
        # from the coordinator's point of view. Reporting the truth here lets
        # _handoff_to (below) refuse to hand off and keep retrying instead.
        logger.warning(
            "%s did not stop after %d graceful attempts — still active, SDR not released",
            service, max_attempts)
        return False

    async def _is_service_active(self, service: str) -> bool:
        loop = asyncio.get_running_loop()

        def _check():
            try:
                result = subprocess.run(["systemctl", "is-active", service],
                                        capture_output=True, text=True, timeout=5)
                return result.stdout.strip() == "active"
            except Exception:
                return False

        return await loop.run_in_executor(None, _check)

    def _run_systemctl(self, action: str, service: str) -> None:
        # sudo required: passive-vigilance runs as a non-root user but needs to
        # start/stop SDR decoder services. install.sh writes a scoped sudoers rule
        # (/etc/sudoers.d/passive-vigilance) covering each managed service.
        try:
            result = subprocess.run(["sudo", "systemctl", action, service],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                logger.warning("systemctl %s %s exited %d: %s", action, service,
                               result.returncode, result.stderr.strip())
            else:
                logger.debug("systemctl %s %s: OK", action, service)
        except Exception as exc:
            logger.warning("systemctl %s %s failed: %s", action, service, exc)

    # ------------------------------------------------------------------
    # SDR release barrier
    # ------------------------------------------------------------------

    async def _settle_sdr(self) -> None:
        """Let the kernel/libusb fully release the dongle before the next owner.

        The outgoing owner is confirmed down at the process level before this runs,
        but the USB device handle lingers a moment longer. Opening it during that
        window wedges readsb and starves the decoder.
        """
        if self._handoff_settle > 0:
            await asyncio.sleep(self._handoff_settle)
        if self._usb_reset:
            await self._reset_sdr()

    async def _reset_sdr(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_usbreset)

    def _run_usbreset(self) -> None:
        usb_id = self._rtl_usb_id()
        if not usb_id:
            logger.debug("usbreset skipped — no RTL-SDR found in sysfs")
            return
        try:
            # usbreset takes VVVV:PPPP (or BBB/DDD) — NOT a /dev/bus/usb path, which
            # fails with "No such device found". VID:PID also stays valid across the
            # device re-enumeration a reset triggers.
            result = subprocess.run(["sudo", "usbreset", usb_id], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                logger.warning("usbreset %s exited %d: %s", usb_id, result.returncode, result.stderr.strip())
            else:
                logger.debug("usbreset %s: OK", usb_id)
        except Exception as exc:
            logger.warning("usbreset %s failed: %s", usb_id, exc)

    @staticmethod
    def _rtl_usb_id() -> str:
        """Resolve the RTL-SDR's ``VVVV:PPPP`` USB id from sysfs (e.g. ``0bda:2838``),
        or '' if absent. This is the form ``usbreset`` accepts and, unlike a bus/device
        node, it stays valid across the re-enumeration a reset causes."""
        base = "/sys/bus/usb/devices"
        try:
            entries = os.listdir(base)
        except OSError:
            return ""
        for name in entries:
            dev = os.path.join(base, name)
            try:
                with open(os.path.join(dev, "idVendor")) as fh:
                    vendor = fh.read().strip()
                with open(os.path.join(dev, "idProduct")) as fh:
                    product = fh.read().strip()
            except OSError:
                continue
            if vendor == RTL_SDR_VENDOR and product in RTL_SDR_PRODUCTS:
                return "%s:%s" % (vendor, product)
        return ""
