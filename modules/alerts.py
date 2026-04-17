"""Alert backends — abstract base and concrete implementations."""

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

def _derive_persist_path(base: Optional[str], kind: str) -> Optional[str]:
    """Return a derived persist path for a per-type rate limiter.

    e.g. ``"data/rate_limits.json"`` + ``"drone"``
    → ``"data/rate_limits_drone.json"``
    """
    if not base:
        return None
    p = Path(base)
    return str(p.with_name(p.stem + f"_{kind}" + p.suffix))


_NTFY_PRIORITY = {
    "low": "low",
    "default": "default",
    "high": "high",
    "urgent": "max",
}


class RateLimiter:
    """Cooldown tracker with optional JSON persistence across restarts.

    Internal timing uses ``time.monotonic()`` for accuracy.  When
    *persist_path* is set, cooldown state is saved as wall-clock ISO
    timestamps so it survives process restarts.  Stale entries (past
    the cooldown window) are ignored on load and trimmed on save.
    """

    def __init__(self, cooldown_seconds: int = 300, persist_path: Optional[str] = None) -> None:
        self._cooldown = cooldown_seconds
        self._persist_path = persist_path
        self._last_alert: dict[str, float] = {}  # key → time.monotonic() value
        if persist_path:
            self._load_state()

    def is_allowed(self, key: str) -> bool:
        """Return True if *key* has not been alerted within the cooldown period.

        Records the current time for the key when returning True and
        persists the updated state to disk if a persist_path is configured.
        """
        now = time.monotonic()
        last = self._last_alert.get(key)
        if last is None or (now - last) >= self._cooldown:
            self._last_alert[key] = now
            if self._persist_path:
                self._save_state()
            return True
        if self._persist_path:
            self._save_state()
        return False

    def reset(self, key: str) -> None:
        """Manually clear a key's cooldown so it can alert immediately."""
        self._last_alert.pop(key, None)
        if self._persist_path:
            self._save_state()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load cooldown state from *persist_path*.

        Reconstructs monotonic timestamps from stored wall-clock datetimes
        by computing elapsed seconds since the stored timestamp.  Keys whose
        cooldown has already expired are ignored.
        """
        try:
            path = Path(self._persist_path)
            if not path.exists():
                return
            data: dict = json.loads(path.read_text(encoding="utf-8"))
            now_real = datetime.now(timezone.utc)
            now_mono = time.monotonic()
            loaded = 0
            for key, ts_str in data.items():
                try:
                    ts = datetime.fromisoformat(ts_str)
                    elapsed = (now_real - ts).total_seconds()
                    if 0 <= elapsed < self._cooldown:
                        # Reconstruct monotonic equivalent so remaining cooldown is correct
                        self._last_alert[key] = now_mono - elapsed
                        loaded += 1
                except Exception:
                    pass
            logger.debug("RateLimiter: loaded %d active cooldowns from %s", loaded, self._persist_path)
        except Exception as exc:
            logger.warning("RateLimiter: could not load state from %s: %s", self._persist_path, exc)

    def _save_state(self) -> None:
        """Save active cooldown state to *persist_path* as wall-clock ISO timestamps."""
        now_real = datetime.now(timezone.utc)
        now_mono = time.monotonic()
        data: dict[str, str] = {}
        for key, mono_ts in self._last_alert.items():
            elapsed = now_mono - mono_ts
            if elapsed < self._cooldown:  # only save entries still within cooldown
                wall_ts = now_real - timedelta(seconds=elapsed)
                data[key] = wall_ts.isoformat()
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("RateLimiter: could not save state to %s: %s", self._persist_path, exc)


class AlertBackend(ABC):
    """Abstract base class for alert delivery backends."""

    @abstractmethod
    def send(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: list[str] = [],
    ) -> bool:
        """Send an alert. Returns True on success."""

    @abstractmethod
    def send_drone_alert(self, detection: dict) -> bool:
        """Format and send a drone RF detection alert."""

    @abstractmethod
    def send_persistence_alert(self, event) -> bool:
        """Format and send a persistence engine DetectionEvent alert."""

    @abstractmethod
    def send_aircraft_alert(self, aircraft: dict) -> bool:
        """Format and send an ADS-B aircraft alert."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if required credentials/config are present."""


class NtfyBackend(AlertBackend):
    """Alert backend that publishes to an ntfy topic via HTTP POST."""

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._topic = os.getenv("NTFY_TOPIC", "")
        self._server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        drone_cooldown = int(os.getenv("DRONE_ALERT_COOLDOWN_SECONDS", "600"))
        persistence_cooldown = int(os.getenv("PERSISTENCE_ALERT_COOLDOWN_SECONDS", "300"))
        aircraft_cooldown = int(os.getenv("AIRCRAFT_ALERT_COOLDOWN_SECONDS", "60"))
        self._drone_limiter = RateLimiter(drone_cooldown, persist_path=_derive_persist_path(persist_path, "drone"))
        self._persistence_limiter = RateLimiter(persistence_cooldown, persist_path=_derive_persist_path(persist_path, "wifi"))
        self._aircraft_limiter = RateLimiter(aircraft_cooldown, persist_path=_derive_persist_path(persist_path, "aircraft"))

    def is_configured(self) -> bool:
        return bool(self._topic)

    def send(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: list[str] = [],
    ) -> bool:
        if not self.is_configured():
            logger.warning("NtfyBackend.send() called but NTFY_TOPIC is not set")
            return False
        url = f"{self._server}/{self._topic}"
        headers = {
            "Title": title,
            "Priority": _NTFY_PRIORITY.get(priority, "default"),
            "Tags": ",".join(tags) if tags else "",
        }
        try:
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
            resp.raise_for_status()
            logger.debug("ntfy alert sent: %s", title)
            return True
        except requests.RequestException as exc:
            logger.error("ntfy send failed: %s", exc)
            return False

    def send_drone_alert(self, detection: dict) -> bool:
        freq = detection.get("freq_mhz", 0)
        key = f"drone:{freq:.0f}mhz"
        if not self._drone_limiter.is_allowed(key):
            logger.debug("drone alert suppressed (rate limit): %s", key)
            return False
        power = detection.get("power_db", 0)
        lat = detection.get("lat", 0.0)
        lon = detection.get("lon", 0.0)
        body = (
            f"Frequency: {freq} MHz | Power: {power} dBm | "
            f"Location: {lat:.4f}, {lon:.4f}"
        )
        return self.send("Drone RF Detected", body, priority="high", tags=["drone", "alert"])

    def send_persistence_alert(self, event) -> bool:
        key = f"mac:{event.mac}"
        if not self._persistence_limiter.is_allowed(key):
            logger.debug("persistence alert suppressed (rate limit): %s", key)
            return False
        priority = "urgent" if event.alert_level == "high" else "high"
        body = (
            f"MAC: {event.mac} | Score: {event.score:.2f} | "
            f"Seen: {event.observation_count} times | "
            f"Locations: {len(event.locations)} | "
            f"Type: {event.device_type}"
        )
        title = f"Persistent Device — {event.alert_level.upper()}"
        return self.send(title, body, priority=priority, tags=["surveillance", "wifi", "alert"])

    def send_aircraft_alert(self, aircraft: dict) -> bool:
        icao = aircraft.get("icao", "unknown")
        key = f"icao:{icao}"
        if not self._aircraft_limiter.is_allowed(key):
            logger.debug("aircraft alert suppressed (rate limit): %s", key)
            return False
        emergency = aircraft.get("emergency", False)
        callsign = aircraft.get("callsign", icao)
        reg = aircraft.get("registration", "N/A")
        operator = aircraft.get("operator", "N/A")
        country = aircraft.get("country", "N/A")
        alt = aircraft.get("altitude", "N/A")
        priority = "urgent" if emergency else "default"
        body = (
            f"ICAO: {icao} | Registration: {reg} | "
            f"Operator: {operator} | Origin: {country} | "
            f"Altitude: {alt}ft | Emergency: {emergency}"
        )
        title = f"Aircraft Alert — {callsign}"
        return self.send(title, body, priority=priority, tags=["aircraft", "adsb"])


class TelegramBackend(AlertBackend):
    """Alert backend that posts to a Telegram chat via the Bot API."""

    _API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        drone_cooldown = int(os.getenv("DRONE_ALERT_COOLDOWN_SECONDS", "600"))
        persistence_cooldown = int(os.getenv("PERSISTENCE_ALERT_COOLDOWN_SECONDS", "300"))
        aircraft_cooldown = int(os.getenv("AIRCRAFT_ALERT_COOLDOWN_SECONDS", "60"))
        self._drone_limiter = RateLimiter(drone_cooldown, persist_path=_derive_persist_path(persist_path, "drone"))
        self._persistence_limiter = RateLimiter(persistence_cooldown, persist_path=_derive_persist_path(persist_path, "wifi"))
        self._aircraft_limiter = RateLimiter(aircraft_cooldown, persist_path=_derive_persist_path(persist_path, "aircraft"))

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: list[str] = [],
    ) -> bool:
        if not self.is_configured():
            logger.warning("TelegramBackend.send() called but credentials are not set")
            return False
        url = self._API_URL.format(token=self._token)
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        text = f"*{title}*\n{body}"
        if tag_str:
            text += f"\n{tag_str}"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.debug("telegram alert sent: %s", title)
            return True
        except requests.RequestException as exc:
            logger.error("telegram send failed: %s", exc)
            return False

    def send_drone_alert(self, detection: dict) -> bool:
        freq = detection.get("freq_mhz", 0)
        key = f"drone:{freq:.0f}mhz"
        if not self._drone_limiter.is_allowed(key):
            return False
        power = detection.get("power_db", 0)
        lat = detection.get("lat", 0.0)
        lon = detection.get("lon", 0.0)
        body = (
            f"Frequency: {freq} MHz | Power: {power} dBm | "
            f"Location: {lat:.4f}, {lon:.4f}"
        )
        return self.send("Drone RF Detected", body, priority="high", tags=["drone", "alert"])

    def send_persistence_alert(self, event) -> bool:
        key = f"mac:{event.mac}"
        if not self._persistence_limiter.is_allowed(key):
            return False
        priority = "urgent" if event.alert_level == "high" else "high"
        body = (
            f"MAC: {event.mac} | Score: {event.score:.2f} | "
            f"Seen: {event.observation_count} times | "
            f"Locations: {len(event.locations)} | "
            f"Type: {event.device_type}"
        )
        title = f"Persistent Device — {event.alert_level.upper()}"
        return self.send(title, body, priority=priority, tags=["surveillance", "wifi", "alert"])

    def send_aircraft_alert(self, aircraft: dict) -> bool:
        icao = aircraft.get("icao", "unknown")
        key = f"icao:{icao}"
        if not self._aircraft_limiter.is_allowed(key):
            return False
        emergency = aircraft.get("emergency", False)
        callsign = aircraft.get("callsign", icao)
        reg = aircraft.get("registration", "N/A")
        operator = aircraft.get("operator", "N/A")
        country = aircraft.get("country", "N/A")
        alt = aircraft.get("altitude", "N/A")
        priority = "urgent" if emergency else "default"
        body = (
            f"ICAO: {icao} | Registration: {reg} | "
            f"Operator: {operator} | Origin: {country} | "
            f"Altitude: {alt}ft | Emergency: {emergency}"
        )
        title = f"Aircraft Alert — {callsign}"
        return self.send(title, body, priority=priority, tags=["aircraft", "adsb"])


class DiscordBackend(AlertBackend):
    """Alert backend that posts to a Discord channel via webhook embed."""

    _PRIORITY_COLORS = {
        "low": 0x95A5A6,
        "default": 0x3498DB,
        "high": 0xE67E22,
        "urgent": 0xE74C3C,
    }

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        drone_cooldown = int(os.getenv("DRONE_ALERT_COOLDOWN_SECONDS", "600"))
        persistence_cooldown = int(os.getenv("PERSISTENCE_ALERT_COOLDOWN_SECONDS", "300"))
        aircraft_cooldown = int(os.getenv("AIRCRAFT_ALERT_COOLDOWN_SECONDS", "60"))
        self._drone_limiter = RateLimiter(drone_cooldown, persist_path=_derive_persist_path(persist_path, "drone"))
        self._persistence_limiter = RateLimiter(persistence_cooldown, persist_path=_derive_persist_path(persist_path, "wifi"))
        self._aircraft_limiter = RateLimiter(aircraft_cooldown, persist_path=_derive_persist_path(persist_path, "aircraft"))

    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def send(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: list[str] = [],
    ) -> bool:
        if not self.is_configured():
            logger.warning("DiscordBackend.send() called but DISCORD_WEBHOOK_URL is not set")
            return False
        color = self._PRIORITY_COLORS.get(priority, self._PRIORITY_COLORS["default"])
        description = body
        if tags:
            description += "\n" + " ".join(f"`{t}`" for t in tags)
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ]
        }
        try:
            resp = requests.post(self._webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.debug("discord alert sent: %s", title)
            return True
        except requests.RequestException as exc:
            logger.error("discord send failed: %s", exc)
            return False

    def send_drone_alert(self, detection: dict) -> bool:
        freq = detection.get("freq_mhz", 0)
        key = f"drone:{freq:.0f}mhz"
        if not self._drone_limiter.is_allowed(key):
            return False
        power = detection.get("power_db", 0)
        lat = detection.get("lat", 0.0)
        lon = detection.get("lon", 0.0)
        body = (
            f"Frequency: {freq} MHz | Power: {power} dBm | "
            f"Location: {lat:.4f}, {lon:.4f}"
        )
        return self.send("Drone RF Detected", body, priority="high", tags=["drone", "alert"])

    def send_persistence_alert(self, event) -> bool:
        key = f"mac:{event.mac}"
        if not self._persistence_limiter.is_allowed(key):
            return False
        priority = "urgent" if event.alert_level == "high" else "high"
        body = (
            f"MAC: {event.mac} | Score: {event.score:.2f} | "
            f"Seen: {event.observation_count} times | "
            f"Locations: {len(event.locations)} | "
            f"Type: {event.device_type}"
        )
        title = f"Persistent Device — {event.alert_level.upper()}"
        return self.send(title, body, priority=priority, tags=["surveillance", "wifi", "alert"])

    def send_aircraft_alert(self, aircraft: dict) -> bool:
        icao = aircraft.get("icao", "unknown")
        key = f"icao:{icao}"
        if not self._aircraft_limiter.is_allowed(key):
            return False
        emergency = aircraft.get("emergency", False)
        callsign = aircraft.get("callsign", icao)
        reg = aircraft.get("registration", "N/A")
        operator = aircraft.get("operator", "N/A")
        country = aircraft.get("country", "N/A")
        alt = aircraft.get("altitude", "N/A")
        priority = "urgent" if emergency else "default"
        body = (
            f"ICAO: {icao} | Registration: {reg} | "
            f"Operator: {operator} | Origin: {country} | "
            f"Altitude: {alt}ft | Emergency: {emergency}"
        )
        title = f"Aircraft Alert — {callsign}"
        return self.send(title, body, priority=priority, tags=["aircraft", "adsb"])


class ConsoleBackend(AlertBackend):
    """Alert backend that prints formatted alerts to stdout.

    Always configured. Useful for testing and development without external services.
    """

    def is_configured(self) -> bool:
        return True

    def send(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: list[str] = [],
    ) -> bool:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(f"[{timestamp}] ALERT [{priority.upper()}]{tag_str}: {title} — {body}")
        return True

    def send_drone_alert(self, detection: dict) -> bool:
        freq = detection.get("freq_mhz", 0)
        power = detection.get("power_db", 0)
        lat = detection.get("lat", 0.0)
        lon = detection.get("lon", 0.0)
        body = (
            f"Frequency: {freq} MHz | Power: {power} dBm | "
            f"Location: {lat:.4f}, {lon:.4f}"
        )
        return self.send("Drone RF Detected", body, priority="high", tags=["drone", "alert"])

    def send_persistence_alert(self, event) -> bool:
        priority = "urgent" if event.alert_level == "high" else "high"
        body = (
            f"MAC: {event.mac} | Score: {event.score:.2f} | "
            f"Seen: {event.observation_count} times | "
            f"Locations: {len(event.locations)} | "
            f"Type: {event.device_type}"
        )
        title = f"Persistent Device — {event.alert_level.upper()}"
        return self.send(title, body, priority=priority, tags=["surveillance", "wifi", "alert"])

    def send_aircraft_alert(self, aircraft: dict) -> bool:
        icao = aircraft.get("icao", "unknown")
        emergency = aircraft.get("emergency", False)
        callsign = aircraft.get("callsign", icao)
        reg = aircraft.get("registration", "N/A")
        operator = aircraft.get("operator", "N/A")
        country = aircraft.get("country", "N/A")
        alt = aircraft.get("altitude", "N/A")
        priority = "urgent" if emergency else "default"
        body = (
            f"ICAO: {icao} | Registration: {reg} | "
            f"Operator: {operator} | Origin: {country} | "
            f"Altitude: {alt}ft | Emergency: {emergency}"
        )
        title = f"Aircraft Alert — {callsign}"
        return self.send(title, body, priority=priority, tags=["aircraft", "adsb"])


_BACKENDS: dict[str, type[AlertBackend]] = {
    "ntfy": NtfyBackend,
    "telegram": TelegramBackend,
    "discord": DiscordBackend,
    "console": ConsoleBackend,
}


class AlertFactory:
    """Creates and returns configured alert backend instances."""

    @staticmethod
    def get_backend(
        backend_name: Optional[str] = None,
        persist_path: Optional[str] = None,
    ) -> AlertBackend:
        """Return the appropriate backend instance.

        Reads ALERT_BACKEND from .env if backend_name is not provided.
        Falls back to ConsoleBackend if the requested backend is not configured.
        Logs a warning if requested backend is unknown or unconfigured.

        Args:
            backend_name: Override the backend name (ignores ALERT_BACKEND env var).
            persist_path: Base path for rate-limiter persistence files.  Pass
                ``None`` (default) for in-memory-only rate limiting.
        """
        if backend_name is None:
            backend_name = os.getenv("ALERT_BACKEND", "console").lower().strip()

        backend_class = _BACKENDS.get(backend_name)
        if backend_class is None:
            logger.warning(
                "Unknown alert backend %r — falling back to ConsoleBackend", backend_name
            )
            return ConsoleBackend()

        # ConsoleBackend has no rate limiters — don't pass persist_path
        if backend_class is ConsoleBackend:
            backend = ConsoleBackend()
        else:
            backend = backend_class(persist_path=persist_path)
        if not backend.is_configured():
            logger.warning(
                "Alert backend %r is not configured — falling back to ConsoleBackend",
                backend_name,
            )
            return ConsoleBackend()

        return backend
