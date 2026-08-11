"""config — startup validation of Passive Vigilance's environment surface.

PV is configured through ~170 environment variables whose values interact in
non-obvious ways (density presets, egregious thresholds, audible windows, SDR
cycle slices, stall timers). Until now nothing validated any of it: a typo'd
name silently fell back to its default, a garbage numeric crashed deep inside
whichever module read it, and an out-of-range value silently distorted
behaviour. This module is the single declarative registry of every knob —
name, type, allowed range/choices — plus the cross-field checks, evaluated
once at startup.

**Validation never blocks startup.** A field node must not brick itself over a
validator bug or an overly strict rule (the systemd watchdog would turn a
refusing process into a restart loop), so findings are *reported*, prominently,
and startup proceeds: a value that would genuinely crash still crashes exactly
where it always did — but now with an explanatory error logged first — and a
value that would silently misbehave is surfaced instead of invisible. The one
existing hard gate (NODE_MODE, resolved fail-loud in main) is unchanged; it is
re-checked here only so the report is complete.

First pass is validation-only by design: the existing lazy ``os.getenv`` read
sites are untouched (tests patch the environment per-call and modules re-read
some knobs at runtime). Migrating consumers onto a typed snapshot is a
follow-up; this registry is that snapshot's schema.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off", "")
_BOOL_ACCEPTED = _TRUE + _FALSE


@dataclass(frozen=True)
class Finding:
    """One validation result: what's wrong with which variable."""

    severity: str  # ERROR | WARNING
    var: str
    message: str


@dataclass(frozen=True)
class Spec:
    """Declared shape of one environment variable."""

    kind: str  # "int" | "float" | "bool" | "str" | "choice"
    choices: Optional[tuple] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    check: Optional[Callable[[str], Optional[str]]] = None  # extra validator -> error msg


def _sdr_cycle_slices(value: str) -> Optional[str]:
    """`name:seconds,...` — names from the known band owners, seconds > 0."""
    known = ("adsb", "ais", "acars", "drone_rf")
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return f"slice {part!r} is not name:seconds"
        name, _, secs = part.partition(":")
        if name.strip() not in known:
            return f"unknown band {name.strip()!r} (known: {', '.join(known)})"
        try:
            if int(secs) <= 0:
                return f"slice {part!r} must have seconds > 0"
        except ValueError:
            return f"slice {part!r} seconds is not an integer"
    return None


def _night_hours(value: str) -> Optional[str]:
    """`HH-HH`, both 0-23 (a wrap like 22-06 is valid)."""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value.strip())
    if not m or not all(0 <= int(h) <= 23 for h in m.groups()):
        return "expected HH-HH with hours 0-23 (e.g. 22-06)"
    return None


def _http_url(value: str) -> Optional[str]:
    if value and not value.startswith(("http://", "https://")):
        return "expected an http(s):// URL"
    return None


def _negative_dbm(value: str) -> Optional[str]:
    """RSSI thresholds are negative dBm; a positive value can never match."""
    try:
        if float(value) > 0:
            return "RSSI thresholds are negative dBm (e.g. -40); a positive value never matches"
    except ValueError:
        pass  # the numeric kind check reports this
    return None


# Shorthand builders keep the registry table readable.
def _i(lo: Optional[float] = 0, hi: Optional[float] = None) -> Spec:
    return Spec("int", lo=lo, hi=hi)


def _f(lo: Optional[float] = 0, hi: Optional[float] = None,
       check: Optional[Callable] = None) -> Spec:
    return Spec("float", lo=lo, hi=hi, check=check)


def _b() -> Spec:
    return Spec("bool")


def _s(check: Optional[Callable] = None) -> Spec:
    return Spec("str", check=check)


def _c(*choices: str) -> Spec:
    return Spec("choice", choices=choices)


_PORT = _i(lo=1, hi=65535)
_FRACTION = _f(lo=0.0, hi=1.0)

# The registry: every environment variable PV reads, by name.
# tests/test_config.py asserts this stays complete against the codebase's
# actual os.getenv calls, so it cannot silently drift.
REGISTRY: dict[str, Spec] = {
    # --- node identity / mode ---
    "NODE_MODE": _c("fixed", "mobile"),
    "NODE_DENSITY": _c("dense", "suburban", "rural"),
    "LOG_LEVEL": _c("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    # --- alerting ---
    "ALERT_BACKEND": _c("ntfy", "telegram", "discord", "console"),
    "NTFY_TOPIC": _s(), "NTFY_SERVER": _s(check=_http_url),
    "TELEGRAM_BOT_TOKEN": _s(), "TELEGRAM_CHAT_ID": _s(),
    "DISCORD_WEBHOOK_URL": _s(check=_http_url),
    "DRONE_ALERT_COOLDOWN_SECONDS": _i(),
    "PERSISTENCE_ALERT_COOLDOWN_SECONDS": _i(),
    "AIRCRAFT_ALERT_COOLDOWN_SECONDS": _i(),
    "EGREGIOUS_ALERT_COOLDOWN_SECONDS": _i(),
    "ALERT_MAX_INFLIGHT": _i(lo=1),
    "WIFI_ALERT_MIN_SCORE": _FRACTION,
    # --- WIDS consumption ---
    "WIDS_ALERTS_ENABLED": _b(),
    "WIDS_MIN_SEVERITY": _i(lo=0, hi=100),
    "WIDS_IGNORE_HEADERS": _s(),
    "WIDS_ALERT_COOLDOWN_SECONDS": _f(),
    # --- scoring: shared / fixed ---
    "WIFI_MONITOR_INTERFACE": _s(),
    "FIXED_BASELINE_HOURS": _f(lo=1),
    "OFF_SCHEDULE_MIN_BASELINE_HOURS": _i(lo=0, hi=24),
    "EGREGIOUS_SIGNAL_DBM": _f(lo=None, check=_negative_dbm),
    "EGREGIOUS_BLE_SIGNAL_DBM": _f(lo=None, check=_negative_dbm),
    "ADAPTATION_POSTURE": _c("off", "conservative", "permissive"),
    "ADAPTATION_SWEEP_ENABLED": _b(),
    "ADAPTATION_SWEEP_INTERVAL_SECONDS": _f(lo=1),
    "APPROACHING_MIN_BASELINE_SAMPLES": _i(),
    "APPROACHING_MIN_RECENT_SAMPLES": _i(),
    "APPROACHING_MIN_DB_MARGIN": _f(),
    "APPROACHING_SIGMA_MARGIN": _f(),
    "APPROACHING_RECENT_EMA_ALPHA": _FRACTION,
    "BASELINE_DB_PATH": _s(),
    # --- scoring: mobile ---
    "PERSISTENCE_ALERT_THRESHOLD": _FRACTION,
    "PERSISTENCE_MIN_LOCATIONS": _i(lo=1),
    "PERSISTENCE_POLL_INTERVAL_SECONDS": _i(lo=1),
    "HANDLE_MAC_RANDOMIZATION": _b(),
    "IGNORE_RANDOMIZED_MACS": _b(),
    "IGNORE_SELF_MACS": _b(),
    "WIFI_RETURN_GAP_SECONDS": _f(),
    # --- identity / fingerprinting ---
    "FP_DISTINCTIVE_MAX_DF": _i(lo=1),
    "FP_MEDIUM_MAX_DF": _i(lo=1),
    "FP_ANCHOR_REFRESH_SECONDS": _f(),
    "PROBE_HISTORY_MAX_MACS": _i(lo=1),
    "PROBE_MAX_SSIDS_PER_MAC": _i(lo=1),
    "CROSS_PHY_LINKING_ENABLED": _b(),
    "COPRESENCE_MIN_POLLS": _i(lo=1),
    "COPRESENCE_MIN_OBS_POLLS": _i(lo=1),
    "COPRESENCE_MIN_FIXTURE_POLLS": _i(lo=1),
    "COPRESENCE_FIXTURE_FRACTION": _FRACTION,
    "COPRESENCE_MIN_JACCARD": _FRACTION,
    "COPRESENCE_RSSI_GATE": _b(),
    "COPRESENCE_MIN_CORR_SAMPLES": _i(lo=2),
    "COPRESENCE_MIN_RSSI_CORR": _f(lo=-1.0, hi=1.0),
    "COPRESENCE_MIN_RSSI_STD": _f(),
    # --- Kismet ---
    "KISMET_HOST": _s(), "KISMET_PORT": _PORT, "KISMET_API_KEY": _s(),
    "KISMET_POLL_INTERVAL_SECONDS": _i(lo=1),
    "KISMET_CONNECT_RETRIES": _i(lo=1),
    "KISMET_CONNECT_RETRY_INTERVAL_SECONDS": _f(),
    "KISMET_ACTIVE_WINDOW_SECONDS": _i(),
    # --- BLE ---
    "BLE_SCANNER_ENABLED": _b(), "BLE_HCI_DEVICE": _s(),
    "BLE_CONNECT_RETRIES": _i(lo=1),
    # --- GPS ---
    "GPS_DEVICE": _s(),
    "GPS_MIN_QUALITY": _c("any", "2d", "3d"),
    "GPS_MAX_HDOP": _f(),
    "GPS_POLL_INTERVAL_SECONDS": _i(lo=1),
    "GPS_STARTUP_TIMEOUT_SECONDS": _i(),
    "GPS_READ_TIMEOUT_SECONDS": _f(),
    "GPS_READER_MIN_INTERVAL": _f(),
    # --- ADS-B / aircraft ---
    "DUMP1090_HOST": _s(), "READSB_URL": _s(check=_http_url),
    "ADSB_POLL_INTERVAL_SECONDS": _i(lo=1),
    "ADSBXLOL_API_KEY": _s(),
    "AIRCRAFT_REGISTRY_DB": _s(),
    "AIRCRAFT_RETENTION_SECONDS": _f(lo=1),
    "AIRCRAFT_RETURN_GAP_SECONDS": _f(),
    "AIRCRAFT_TRACK_MIN_SECONDS": _f(),
    "AIRCRAFT_TRACK_MIN_METERS": _f(),
    "AIRCRAFT_TRACK_MAX_POINTS": _i(lo=2),
    "CONTACT_TRACK_MAX": _i(lo=1),
    # --- AIS ---
    "AIS_ENABLED": _b(), "AIS_SERVICE": _s(),
    "AIS_UDP_HOST": _s(), "AIS_UDP_PORT": _PORT,
    "AIS_POLL_INTERVAL_SECONDS": _i(lo=1),
    "AIS_SLICE_SECONDS": _i(lo=1),
    "AIS_MAX_RANGE_KM": _f(),
    # --- ACARS ---
    "ACARS_ENABLED": _b(), "ACARS_SERVICE": _s(),
    "ACARS_UDP_HOST": _s(), "ACARS_UDP_PORT": _PORT,
    "ACARS_POLL_INTERVAL_SECONDS": _i(lo=1),
    "ACARS_TRIGGER_SECONDS": _f(lo=1),
    "ACARS_WINDOW_SECONDS": _f(lo=1),
    "ACARS_MAX_WINDOWS_PER_CYCLE": _i(),
    "ACARS_POSITION_MATCH_KM": _f(),
    # --- SDR ---
    "SDR_MODE": _c("auto", "shared", "dedicated"),
    "SDR_CYCLE_SLICES": _s(check=_sdr_cycle_slices),
    "ADSB_SLICE_SECONDS": _i(lo=1),
    "SDR_HANDOFF_SETTLE_SECONDS": _f(),
    "SDR_HANDOFF_USB_RESET": _b(),
    # --- DroneRF (retired, kept for reversibility) ---
    "DRONE_RF_ENABLED": _b(),
    "DRONE_RF_SLICE_SECONDS": _i(lo=1),
    "DRONE_POLL_INTERVAL_SECONDS": _i(lo=1),
    "DRONE_POWER_THRESHOLD_DB": _f(lo=None),
    "DRONE_RF_REST_SECONDS": _i(),
    "DRONE_RF_MAX_TEMP_C": _f(),
    "DRONE_RF_MIN_SWEEPS": _i(lo=1),
    "DRONE_RF_MAX_CRASHES": _i(lo=1),
    "DRONE_RF_CRASH_WINDOW_S": _f(lo=1),
    "DRONE_RF_MONITOR_INTERVAL_S": _f(),
    # --- Remote ID ---
    "REMOTE_ID_POLL_INTERVAL_SECONDS": _i(lo=1),
    # --- entity store ---
    "ENTITY_DB_PATH": _s(),
    "ENTITY_OBSERVATION_RETENTION_DAYS": _i(),
    "ENTITY_OBSERVATION_MAX_ROWS": _i(),
    "ENTITY_PRUNE_INTERVAL_SECONDS": _i(lo=1),
    "ENTITY_PRUNE_BATCH_ROWS": _i(lo=1),
    "ENTITY_PRUNE_TIME_BUDGET_S": _f(),
    "ENTITY_WAL_CHECKPOINT_SECONDS": _i(),
    "ENTITY_AUDIBLE_WINDOW_SECONDS": _i(),
    "ENTITY_ASYNC_WRITES": _b(),
    "ENTITY_WRITE_QUEUE_MAX": _i(lo=1),
    "ENTITY_RETURN_MIN_GAP_SECONDS": _f(),
    "ENTITY_ROLLUP_ENABLED": _b(),
    "ENTITY_ROLLUP_HOUR_UTC": _i(lo=0, hi=23),
    "ENTITY_ROLLUP_BATCH_ROWS": _i(lo=1),
    "ENTITY_ROLLUP_TIME_BUDGET_S": _f(),
    "ENTITY_SIGHTING_RETENTION_DAYS": _i(),
    "VISITOR_ALERT_MIN_OBS": _i(lo=1),
    # --- GUI ---
    "GUI_ENABLED": _b(), "GUI_HOST": _s(), "GUI_PORT": _PORT,
    "GUI_TOKEN": _s(),
    "GUI_BIND_RETRIES": _i(lo=1), "GUI_BIND_RETRY_SECONDS": _f(),
    "GUI_HISTORY_LIMIT": _i(lo=1), "GUI_HISTORY_MAX_SESSIONS": _i(lo=1),
    # --- recon-pair survey ---
    "SURVEY_ENABLED": _b(),
    "SURVEY_NODE_ID": _s(), "SURVEY_FIXED_URL": _s(check=_http_url),
    "SURVEY_TOKEN": _s(),
    "SURVEY_SYNC_INTERVAL_SECONDS": _f(lo=1),
    "SURVEY_AUTOTASK": _b(),
    "SURVEY_AUTOTASK_MIN_LEVEL": _c("suspicious", "likely", "high"),
    "SURVEY_NIGHT_HOURS": _s(check=_night_hours),
    "SURVEY_CLUSTER_METERS": _f(lo=1),
    "SURVEY_IMMEDIATE_METERS": _f(lo=1),
    "SURVEY_NEIGHBORHOOD_METERS": _f(lo=1),
    "SURVEY_VISIT_GAP_SECONDS": _f(),
    "SURVEY_MIN_PATROL_POLLS": _i(lo=1),
    "SURVEY_PATROL_MAX_HOURS": _f(lo=1),
    "SURVEY_OBS_RETENTION_DAYS": _i(),
    "SURVEY_WARDRIVE_RETENTION_DAYS": _i(),
    # --- orchestrator / reliability ---
    "SESSION_OUTPUT_DIR": _s(),
    "HEALTH_BANNER_INTERVAL_SECONDS": _i(lo=1),
    "MAX_RECONNECT_ATTEMPTS": _i(lo=1),
    "RECONNECT_INTERVAL_SECONDS": _i(),
    "SENSOR_STALL_SECONDS": _f(lo=1),
    "SENSOR_DATA_STALL_SECONDS": _f(lo=1),
    "WATCHDOG_INTERVAL_SECONDS": _f(lo=1),
    "WATCHDOG_MAX_RESTARTS": _i(lo=1),
    "WATCHDOG_RESTART_WINDOW_S": _f(lo=1),
    "WATCHDOG_DATA_SENSORS": _s(),
    "BEACON_CAPTURE_ENABLED": _b(),
    "NOTIFY_SOCKET": _s(),  # systemd's, not PV's — accepted, never flagged
    "OUI_MANUF_PATH": _s(),
    # --- outputs ---
    "WIGLE_API_NAME": _s(), "WIGLE_API_KEY": _s(),
}

# .env keys that belong to other tools sharing the file (never typo-flagged).
_FOREIGN_KEY_PREFIXES = ("INSTALL_",)

_BACKEND_CREDENTIALS: dict[str, tuple] = {
    "ntfy": ("NTFY_TOPIC",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "discord": ("DISCORD_WEBHOOK_URL",),
}


def _check_value(var: str, spec: Spec, raw: str) -> list:
    """Validate one present variable's value against its spec."""
    findings = []
    value = raw.strip()
    if spec.kind == "int":
        try:
            n = int(value)
            if spec.lo is not None and n < spec.lo:
                findings.append(Finding(ERROR, var, f"{n} is below minimum {int(spec.lo)}"))
            if spec.hi is not None and n > spec.hi:
                findings.append(Finding(ERROR, var, f"{n} is above maximum {int(spec.hi)}"))
        except ValueError:
            findings.append(Finding(ERROR, var, f"{raw!r} is not an integer"))
    elif spec.kind == "float":
        try:
            n = float(value)
            if spec.lo is not None and n < spec.lo:
                findings.append(Finding(ERROR, var, f"{n} is below minimum {spec.lo}"))
            if spec.hi is not None and n > spec.hi:
                findings.append(Finding(ERROR, var, f"{n} is above maximum {spec.hi}"))
        except ValueError:
            findings.append(Finding(ERROR, var, f"{raw!r} is not a number"))
    elif spec.kind == "bool":
        if value.lower() not in _BOOL_ACCEPTED:
            findings.append(Finding(
                WARNING, var,
                f"{raw!r} is not a recognised boolean — it will be treated as FALSE "
                f"(use one of: {', '.join(_TRUE)} / {', '.join(v for v in _FALSE if v)})"))
    elif spec.kind == "choice":
        # NODE_MODE and LOG_LEVEL are case-normalised by their consumers.
        if value and value.lower() not in tuple(c.lower() for c in spec.choices):
            findings.append(Finding(
                ERROR, var,
                f"{raw!r} is not one of: {', '.join(spec.choices)}"))
    if spec.check is not None and value:
        msg = spec.check(value)
        if msg:
            findings.append(Finding(ERROR, var, msg))
    return findings


def _cross_field_checks(env) -> list:
    """The interactions no single-variable check can see."""
    findings = []

    # NODE_MODE is required (the fail-loud design) — main refuses scoring
    # without it; the report should say so too rather than stay silent.
    if not (env.get("NODE_MODE") or "").strip():
        findings.append(Finding(
            ERROR, "NODE_MODE",
            "not set — the node will refuse to enter scoring (set fixed or mobile)"))

    # A real backend without its credentials pages nobody (#191).
    backend = (env.get("ALERT_BACKEND") or "console").strip().lower()
    for slot in _BACKEND_CREDENTIALS.get(backend, ()):
        if not (env.get(slot) or "").strip():
            findings.append(Finding(
                ERROR, slot,
                f"ALERT_BACKEND={backend} needs {slot} — without it the node "
                f"falls back to console and NO ALERTS PAGE "
                f"(validate with scripts/send_test_alert.py)"))

    # The #232 footgun: 0 disables the recency filter, so every device ever
    # heard stays in every poll — smeared baselines (fixed) or phantom
    # followers (mobile).
    raw = (env.get("KISMET_ACTIVE_WINDOW_SECONDS") or "").strip()
    if raw == "0":
        findings.append(Finding(
            WARNING, "KISMET_ACTIVE_WINDOW_SECONDS",
            "0 disables the recency filter — baselines smear (#232) and mobile "
            "scoring sees phantom followers; only set 0 deliberately"))

    return findings


def _typo_check(dotenv_keys) -> list:
    """Flag .env keys PV will never read — a typo'd knob silently does nothing."""
    findings = []
    for key in dotenv_keys:
        if key in REGISTRY or key.startswith(_FOREIGN_KEY_PREFIXES):
            continue
        close = difflib.get_close_matches(key, REGISTRY.keys(), n=1, cutoff=0.8)
        hint = f" — did you mean {close[0]}?" if close else ""
        findings.append(Finding(
            WARNING, key,
            f"not a recognised setting; it has no effect{hint}"))
    return findings


def validate_environment(env=None, dotenv_path: str = ".env") -> list:
    """Validate the whole configuration surface; return all findings at once.

    *env* defaults to ``os.environ`` (after main's ``load_dotenv()``). The
    typo check reads *dotenv_path* separately because only the keys the
    operator wrote in ``.env`` should be checked for typos — the process
    environment is full of foreign variables.
    """
    if env is None:
        env = os.environ
    findings: list = []
    for var, spec in REGISTRY.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue  # absent/empty -> the consumer's default applies
        findings.extend(_check_value(var, spec, raw))
    findings.extend(_cross_field_checks(env))
    try:
        from dotenv import dotenv_values
        if os.path.exists(dotenv_path):
            findings.extend(_typo_check(dotenv_values(dotenv_path).keys()))
    except Exception:
        pass  # typo detection is best-effort; never fail validation over it
    return findings


def report(findings: list, log: logging.Logger = logger) -> None:
    """Log the findings as one prominent startup block (errors first)."""
    if not findings:
        log.info("Configuration validated: %d settings checked, no findings",
                 len(REGISTRY))
        return
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    log.error("─" * 54)
    log.error("── CONFIGURATION PROBLEMS (%d error(s), %d warning(s)) ──",
              len(errors), len(warnings))
    for f in errors:
        log.error("  %s: %s", f.var, f.message)
    for f in warnings:
        log.warning("  %s: %s", f.var, f.message)
    log.error("Startup continues (a config problem must not brick a field "
              "node) — fix the above in .env and restart.")
    log.error("─" * 54)
