"""ACARS module — passive aviation datalink decode via an acarsdec/dumpvdl2 JSON feed.

OPTIONAL / best-effort. ACARS is aviation VHF (legacy ~131 MHz via acarsdec; modern
VDL Mode 2 ~136 MHz via dumpvdl2) and will not receive on a 1090 MHz ADS-B antenna,
so it is OFF by default (``ACARS_ENABLED``). **ACARS is plaintext — this DECODES it,
it does not "decrypt".** It is also a SHARED broadcast channel: you receive every
aircraft in range, not a chosen target; the orchestrator correlates a decoded
message back to a live ADS-B contact by tail number / flight-id.

The decoder runs as a systemd service, invoked by the SDR coordinator's
``request_band_window("acars", …)`` when an ADS-B contact has been held >30 s (on a
single dongle), or continuously on a dedicated VHF dongle. This module just listens
on a localhost UDP socket for the decoder's line-delimited JSON and buffers parsed
messages for the orchestrator to drain — the same contract AIS/DroneRF/ADS-B use.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from modules.sdr_utils import is_rtl_sdr_present

load_dotenv()

logger = logging.getLogger(__name__)

ACARS_UDP_HOST = os.getenv("ACARS_UDP_HOST", "127.0.0.1")
ACARS_UDP_PORT = int(os.getenv("ACARS_UDP_PORT", "5555"))

# Origin/destination structured-field aliases across acarsdec / dumpvdl2 / VDL2.
_ORIGIN_KEYS = ("depa", "dep", "origin", "orig")
_DEST_KEYS = ("dsta", "dst", "destination", "dest", "arr")

# Two position encodings that show up in ACARS position-report free text. Both are
# deliberately strict (a decimal point is required, hemispheres explicit) so noise
# text never yields a bogus fix. Anything ambiguous returns no position.
#   decimal:      "N47.1234 W122.4567"
#   degree-minute:"N4712.3W12227.4"  (DDMM.m / DDDMM.m)
_POS_DECIMAL_RE = re.compile(
    r"(?P<lath>[NS])\s*(?P<lat>\d{1,2}\.\d+)\s*[, ]?\s*(?P<lonh>[EW])\s*(?P<lon>\d{1,3}\.\d+)"
)
_POS_DEGMIN_RE = re.compile(
    r"(?P<lath>[NS])(?P<latd>\d{2})(?P<latm>\d{2}\.\d+)(?P<lonh>[EW])(?P<lond>\d{3})(?P<lonm>\d{2}\.\d+)"
)
# AOC position-report encoding: implied-decimal degrees with NO decimal point, e.g.
# "POSN21207W157466" = 21.207 N, 157.466 W (lat DD + 3 fractional digits, lon DDD + 3).
# NOT degree-minutes — verified against real reports ("POSA1N21318W157701" → 157.701,
# which as minutes would be an impossible 70.1'). An optional 2-char report subtype may
# sit between POS and the hemisphere. Anchored on "POS" so it can't fire on stray digits.
_POS_ACARS_RE = re.compile(
    r"POS(?:[A-Z0-9]{2})?(?P<lath>[NS])(?P<latd>\d{2})(?P<latf>\d{3})"
    r"(?P<lonh>[EW])(?P<lond>\d{3})(?P<lonf>\d{3})"
)


def _first_str(*sources) -> Optional[str]:
    """First non-empty stripped string among ``(mapping, keys)`` source pairs."""
    for mapping, keys in sources:
        if not isinstance(mapping, dict):
            continue
        for k in keys:
            v = mapping.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _extract_position(acars: dict, outer: dict, text: Optional[str]):
    """Return ``(lat, lon)`` for a message that carries a position, else ``(None, None)``.

    Structured numeric lat/lon fields (on the ACARS block or the outer decoder
    object) win; otherwise a strict pattern match against the message text. A
    result is only returned when both are in range, so a partial/garbled match is
    dropped rather than mis-placing a contact.
    """
    for src in (acars, outer):
        if not isinstance(src, dict):
            continue
        lat = _num(src.get("lat", src.get("latitude")))
        lon = _num(src.get("lon", src.get("longitude")))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    if text:
        m = _POS_DECIMAL_RE.search(text)
        if m:
            lat = float(m.group("lat")) * (1 if m.group("lath") == "N" else -1)
            lon = float(m.group("lon")) * (1 if m.group("lonh") == "E" else -1)
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
        m = _POS_DEGMIN_RE.search(text)
        if m:
            lat = (int(m.group("latd")) + float(m.group("latm")) / 60.0) * (
                1 if m.group("lath") == "N" else -1)
            lon = (int(m.group("lond")) + float(m.group("lonm")) / 60.0) * (
                1 if m.group("lonh") == "E" else -1)
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
        m = _POS_ACARS_RE.search(text)
        if m:
            lat = (int(m.group("latd")) + int(m.group("latf")) / 1000.0) * (
                1 if m.group("lath") == "N" else -1)
            lon = (int(m.group("lond")) + int(m.group("lonf")) / 1000.0) * (
                1 if m.group("lonh") == "E" else -1)
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
    return None, None


# ---------------------------------------------------------------------------
# Human-friendly classification (categorize + extract known fields)
# ---------------------------------------------------------------------------
#
# ACARS content is only partly standardized — much of the free text on the common
# labels (H1/37/5Z) is airline-proprietary and undocumented. We do NOT try to decode
# that; instead we (1) name the ARINC label where we can, (2) sort the message into a
# human category from reliable signals, and (3) surface the pieces we CAN parse
# (position, route, flight, OOOI times) as labeled fields. Anything we can't place
# stays "Free text / other" with the raw text shown verbatim — no fake decoding.

# ARINC-620 downlink labels we can name with reasonable confidence. The label is a
# secondary hint; the category below does the real grouping. Unknown labels fall back
# to a generic descriptor rather than a guess.
_LABEL_NAMES = {
    "H1": "Message / airline data",
    "5Z": "Airline-defined downlink",
    "37": "Airline-defined downlink",
    "SA": "Media advisory (link setup)",
    "SQ": "Squitter / positioning",
    "SB": "Departure/arrival (OOOI)",
    "Q0": "Link test",
    "_d": "Link management",
    "_j": "Link management",
    "10": "Airline downlink",
    "80": "Airline downlink",
    "B9": "ATS report",
}

CATEGORY_POSITION = "Position report"
CATEGORY_PERFORMANCE = "Performance / engine"
CATEGORY_OOOI = "Flight progress (OOOI)"
CATEGORY_ROUTE = "Route / dispatch"
CATEGORY_LINK = "Link management"
CATEGORY_FREE = "Free text / other"

# Labels that are pure link/media management (no user-facing content).
_LINK_LABELS = {"_d", "_j", "sa", "q0", "sm", "sv"}
# Engine / performance report markers seen in the free text (e.g. an "APM" report or
# an ACMF snapshot). Word-boundaried so they don't fire on noise.
_PERF_RE = re.compile(r"\b(APM|ACMF|PERF|ENGINE|FUEL FLOW)\b", re.I)
# OOOI (Out/Off/On/In) event paired with a clock time; we require two or more pairs so
# an incidental "ON"/"IN" in free text can't be mistaken for a flight-progress report.
_OOOI_PAIR_RE = re.compile(r"\b(OUT|OFF|ON|IN)\s*(\d{4})\b", re.I)


def _fmt_pos(lat, lon) -> str:
    """A position as a compact hemisphere string, e.g. 'N21.207, W157.466'."""
    return (f"{'N' if lat >= 0 else 'S'}{abs(lat):.3f}, "
            f"{'E' if lon >= 0 else 'W'}{abs(lon):.3f}")


# Token shapes inside a comma-delimited AOC position report.
_HHMMSS_RE = re.compile(r"^\d{6}$")               # a clock time HHMMSS
_WPT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")     # a named fix / waypoint
_LEVEL_RE = re.compile(r"^\d{2,3}$")              # a flight level (hundreds of feet)


def _hhmmss(t: str) -> str:
    return f"{t[0:2]}:{t[2:4]}:{t[4:6]}"


def _position_report_fields(text: str) -> list:
    """Break out the common AOC position report beyond the raw fix:
    ``POS<coords>,<fix>,<time>,<level>,<next fix>,<ETA>,<following fix>,…``.

    Positional but DEFENSIVE — each field is emitted only if its token matches the
    expected shape, so a report in a different airline dialect degrades to just the
    position instead of mislabeling. The trailing airline-specific fields (fuel/wind/
    checksum) are intentionally left in the raw text, not guessed at.
    """
    m = _POS_ACARS_RE.search(text)
    if not m:
        return []
    toks = [t.strip() for t in text[m.end():].split(",")]
    while toks and toks[0] == "":            # drop the empty right after the coords
        toks.pop(0)
    fields = []
    if len(toks) >= 2 and _WPT_RE.match(toks[0]) and _HHMMSS_RE.match(toks[1]):
        fields.append({"name": "Over", "value": f"{toks[0]} at {_hhmmss(toks[1])}"})
        if len(toks) >= 3 and _LEVEL_RE.match(toks[2]):
            fields.append({"name": "Flight level", "value": f"FL{int(toks[2]):03d}"})
        if len(toks) >= 5 and _WPT_RE.match(toks[3]) and _HHMMSS_RE.match(toks[4]):
            fields.append({"name": "Next", "value": f"{toks[3]} · ETA {_hhmmss(toks[4])}"})
        if len(toks) >= 6 and _WPT_RE.match(toks[5]):
            fields.append({"name": "Then", "value": toks[5]})
    return fields


def classify(label, text, flight, origin, destination, lat, lon):
    """Sort a parsed message into a human category and pull the fields we can parse.

    Returns ``(category, fields)`` where ``fields`` is an ordered list of
    ``{"name", "value"}`` dicts safe to display. Signals are checked strongest-first;
    proprietary payloads with no reliable signal land in ``Free text / other``.
    """
    label_l = (label or "").strip().lower()
    txt = text or ""

    # Fields we can extract regardless of category, most identifying first.
    fields = []
    if flight:
        fields.append({"name": "Flight", "value": flight})
    if origin and destination:
        fields.append({"name": "Route", "value": f"{origin}→{destination}"})
    if lat is not None and lon is not None:
        fields.append({"name": "Position", "value": _fmt_pos(lat, lon)})

    oooi_pairs = _OOOI_PAIR_RE.findall(txt)

    # Category — strongest, least-ambiguous signal wins.
    if lat is not None and lon is not None:
        category = CATEGORY_POSITION
        # A textbook AOC position report also carries a fix/time/level/next-fix
        # sequence — break out the pieces we can identify reliably.
        fields.extend(_position_report_fields(txt))
    elif _PERF_RE.search(txt):
        category = CATEGORY_PERFORMANCE
    elif len(oooi_pairs) >= 2 or label_l == "sb":
        category = CATEGORY_OOOI
        for event, clock in oooi_pairs:
            fields.append({"name": event.upper(),
                           "value": f"{clock[:2]}:{clock[2:]}"})
    elif origin and destination:
        category = CATEGORY_ROUTE
    elif label_l in _LINK_LABELS or not txt.strip():
        category = CATEGORY_LINK
    else:
        category = CATEGORY_FREE
    return category, fields


# ---------------------------------------------------------------------------
# Application-layer decode (CPDLC / ADS-C / MIAM / media advisory)
# ---------------------------------------------------------------------------
#
# The parts of ACARS that ARE standardized — CPDLC (controller/pilot datalink),
# ADS-C (surveillance contracts), MIAM (file transfer), and media advisories —
# are decoded by libacars, which dumpvdl2/acarsdec emit as a nested tree inside
# the ACARS object. We read that decoded tree rather than re-parsing free text.
#
# Extraction is deliberately DEFENSIVE: we locate app nodes and pull fields by
# key name via a recursive scan, so a libacars version that renames or re-nests a
# field degrades to naming the message type (still a readability win) instead of
# crashing or silently vanishing. This mirrors the module's "name what we can,
# never fake-decode" stance — every field shown is a value libacars produced.

CATEGORY_CPDLC = "CPDLC (controller/pilot)"
CATEGORY_ADSC = "ADS-C (surveillance)"
CATEGORY_MIAM = "MIAM (file transfer)"
CATEGORY_MEDIA_ADV = "Link status (media advisory)"

# Scalar leaf keys worth surfacing from a decoded app subtree → display name.
# Position (lat/lon) is intentionally excluded: it flows through classify() as the
# single "Position" field so ADS-C reports don't show it twice.
_APP_SCALAR_NAMES = {
    "alt": "Altitude", "altitude": "Altitude",
    "level": "Level", "flight_level": "Flight level",
    "heading": "Heading", "track": "Track", "true_track": "Track",
    "speed": "Speed", "ground_speed": "Ground speed",
    "vspd": "Vertical speed", "vertical_speed": "Vertical speed",
    "freq": "Frequency", "frequency": "Frequency",
    "eta": "ETA",
    "temp": "Temperature", "sat": "SAT",
    "msg_id": "Msg id", "msg_ref": "Reply to",
}

# Media-advisory link-state and link-type codes (ARINC 618 / libacars).
_LINK_STATE = {"E": "Established", "L": "Lost"}
_LINK_TYPE = {
    "V": "VHF ACARS", "2": "VDL Mode 2", "X": "VDL",
    "S": "SATCOM", "H": "HF", "G": "GlobalStar SATCOM",
    "C": "ICO SATCOM", "I": "Inmarsat SATCOM",
}


def _walk(obj):
    """Yield ``(key, scalar_value)`` for every scalar leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield k, v
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _find_node(tree, target: str):
    """First value stored under a key equal to *target* (case-insensitive), searched
    recursively through nested dicts/lists. ``None`` if absent."""
    if isinstance(tree, dict):
        for k, v in tree.items():
            if isinstance(k, str) and k.lower() == target:
                return v
        for v in tree.values():
            found = _find_node(v, target)
            if found is not None:
                return found
    elif isinstance(tree, list):
        for item in tree:
            found = _find_node(item, target)
            if found is not None:
                return found
    return None


def _first_scalar(tree, keys):
    """First scalar leaf whose key matches one of *keys* (case-insensitive)."""
    keyset = {k.lower() for k in keys}
    for k, v in _walk(tree):
        if isinstance(k, str) and k.lower() in keyset:
            return v
    return None


def _humanize(s: str) -> str:
    """A libacars identifier (``level_change``) → a readable phrase (``Level change``)."""
    out = re.sub(r"[_\-]+", " ", str(s)).strip()
    return out[:1].upper() + out[1:] if out else out


def _app_scalar_fields(subtree, limit: int = 8) -> list:
    """Surface a bounded, de-duplicated set of recognized scalar values as fields."""
    fields, seen = [], set()
    for k, v in _walk(subtree):
        name = _APP_SCALAR_NAMES.get(k.lower()) if isinstance(k, str) else None
        if not name or name in seen or isinstance(v, bool) or v is None or v == "":
            continue
        fields.append({"name": name, "value": str(v)})
        seen.add(name)
        if len(fields) >= limit:
            break
    return fields


def _cpdlc_direction(tree) -> Optional[str]:
    if _find_node(tree, "atc_uplink_msg") is not None or _find_node(tree, "atc_uplink") is not None:
        return "Uplink (ATC → aircraft)"
    if _find_node(tree, "atc_downlink_msg") is not None or _find_node(tree, "atc_downlink") is not None:
        return "Downlink (aircraft → ATC)"
    return None


def _cpdlc_elements(cpdlc) -> list:
    """Human phrases for each CPDLC message element. libacars serializes the ASN.1
    CHOICE either as ``{"choice": "<name>", ...}`` or as ``{"<name>": {...}}`` — handle
    both, and cap the count so a long clearance stays readable."""
    elems = _find_node(cpdlc, "msg_element")
    if isinstance(elems, dict):
        elems = [elems]
    out = []
    if isinstance(elems, list):
        for e in elems:
            if not isinstance(e, dict):
                continue
            if e.get("choice"):
                out.append(_humanize(str(e["choice"])))
            else:
                for k in e:                       # first alternative name
                    out.append(_humanize(k))
                    break
    return out[:5]


def _media_adv_fields(media) -> list:
    fields = []
    state = _first_scalar(media, ("state", "link_state"))
    if state is not None and not isinstance(state, (dict, list)):
        s = str(state)
        fields.append({"name": "Link state", "value": _LINK_STATE.get(s[:1].upper(), s)})
    link = _first_scalar(media, ("current_link", "link", "established_link"))
    if link is not None and not isinstance(link, (dict, list)):
        s = str(link)
        fields.append({"name": "Current link", "value": _LINK_TYPE.get(s[:1].upper(), s)})
    avail = _find_node(media, "available_links")
    if isinstance(avail, list) and avail:
        vals = [_LINK_TYPE.get(str(x)[:1].upper(), str(x))
                for x in avail if not isinstance(x, (dict, list))]
        if vals:
            fields.append({"name": "Available", "value": ", ".join(vals)})
    t = _first_scalar(media, ("time", "timestamp"))
    if t is not None and not isinstance(t, (dict, list)):
        fields.append({"name": "Time", "value": str(t)})
    return fields


def _decode_app(acars: dict):
    """If libacars decoded a standardized application payload into the ACARS object,
    return ``{"category", "fields", "lat", "lon"}``; otherwise ``None``.

    ``lat``/``lon`` are populated only by ADS-C (which carries a precise fix); the
    caller adopts them when the message had no other position.
    """
    if not isinstance(acars, dict):
        return None

    adsc = _find_node(acars, "adsc")
    if adsc is not None:
        lat = _num(_first_scalar(adsc, ("lat", "latitude")))
        lon = _num(_first_scalar(adsc, ("lon", "longitude")))
        if not (lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180):
            lat = lon = None
        return {"category": CATEGORY_ADSC, "fields": _app_scalar_fields(adsc),
                "lat": lat, "lon": lon}

    cpdlc = _find_node(acars, "cpdlc")
    if cpdlc is not None:
        fields = []
        direction = _cpdlc_direction(acars)
        if direction:
            fields.append({"name": "Direction", "value": direction})
        fields.extend({"name": "Message", "value": e} for e in _cpdlc_elements(cpdlc))
        fields.extend(_app_scalar_fields(cpdlc))
        return {"category": CATEGORY_CPDLC, "fields": fields, "lat": None, "lon": None}

    media = _find_node(acars, "media_adv") or _find_node(acars, "media-adv")
    if media is not None:
        return {"category": CATEGORY_MEDIA_ADV, "fields": _media_adv_fields(media),
                "lat": None, "lon": None}

    miam = _find_node(acars, "miam")
    if miam is not None:
        return {"category": CATEGORY_MIAM, "fields": _app_scalar_fields(miam),
                "lat": None, "lon": None}

    return None


def reclassify(rec: dict) -> dict:
    """Backfill category / label_name / fields (and a compact-format position) onto a
    stored ACARS record that predates classification, derived from its own raw fields.

    Returns a shallow copy so the caller's cached/stored dict is never mutated.
    Idempotent: a record already carrying a category is returned unchanged. Used at GUI
    serve time so historical messages show the same breakout as freshly-decoded ones.
    """
    if not isinstance(rec, dict) or rec.get("category"):
        return rec
    text = rec.get("text")
    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is None or lon is None:
        lat, lon = _extract_position(rec, {}, text)
    label = rec.get("label")
    category, fields = classify(label, text, rec.get("flight_id"),
                                rec.get("origin"), rec.get("destination"), lat, lon)
    out = dict(rec)
    out["lat"], out["lon"] = lat, lon
    out["category"] = category
    out["label_name"] = _LABEL_NAMES.get(label, "Airline / other") if label else None
    out["fields"] = fields
    return out


class _ACARSDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_line) -> None:
        self._on_line = on_line

    def datagram_received(self, data: bytes, addr) -> None:
        for line in data.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if line:
                self._on_line(line)

    def error_received(self, exc) -> None:  # pragma: no cover - transport noise
        logger.debug("ACARS UDP error: %s", exc)


class ACARSModule:
    """Consume acarsdec/dumpvdl2 JSON over UDP and buffer parsed datalink messages."""

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._host = ACARS_UDP_HOST
        self._port = ACARS_UDP_PORT
        self._transport: Optional[asyncio.BaseTransport] = None
        self._buffer: list[dict] = []
        # Mirror the other SDR bands so the coordinator/GUI treat ACARS uniformly.
        self.can_scan: bool = True
        self.auto_disabled: bool = False

    def is_hardware_present(self) -> bool:
        return is_rtl_sdr_present()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _ACARSDatagramProtocol(self._ingest),
            local_addr=(self._host, self._port),
        )
        logger.info("ACARSModule listening for decoder JSON on %s:%d",
                    self._host, self._port)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        logger.info("ACARSModule closed")

    def _ingest(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        det = self._parse(msg)
        if det is not None:
            self._buffer.append(det)

    @staticmethod
    def _parse(msg: dict) -> Optional[dict]:
        """Normalize an acarsdec OR dumpvdl2 JSON object → a datalink message.

        Both decoders nest the ACARS payload differently:
        - acarsdec: flat-ish, keys include ``tail``/``reg``, ``flight``, ``label``, ``text``.
        - dumpvdl2: ``{"vdl2": {"avlc": {"acars": {"reg","flight","label","msg_text"}}}}``.
        We extract a tail, flight-id, label and free text from whichever is present.
        """
        acars = msg
        # dumpvdl2 nesting → dig down to the inner acars block if present.
        if "vdl2" in msg:
            acars = (((msg.get("vdl2") or {}).get("avlc") or {}).get("acars") or {})
        if not isinstance(acars, dict) or not acars:
            return None

        tail = (acars.get("tail") or acars.get("reg") or "").strip() or None
        flight = (acars.get("flight") or acars.get("flight_id") or "").strip() or None
        label = (acars.get("label") or "").strip() or None
        text = acars.get("text")
        if text is None:
            text = acars.get("msg_text")
        if isinstance(text, str):
            text = text.strip() or None
        # A message with no identity AND no content is noise — drop it.
        if tail is None and flight is None and text is None:
            return None
        # Enrichment fields (all optional): the origin/destination airports the
        # airframe declares, and a position report when the message carries one.
        # These are what tie a message to a contact beyond the tail/callsign and
        # what fill out the aircraft row.
        origin = _first_str((acars, _ORIGIN_KEYS), (msg, _ORIGIN_KEYS))
        destination = _first_str((acars, _DEST_KEYS), (msg, _DEST_KEYS))
        lat, lon = _extract_position(acars, msg, text)
        # Application-layer decode: if the decoder (libacars, via dumpvdl2/acarsdec)
        # decoded a standardized payload — CPDLC / ADS-C / MIAM / media advisory — read
        # its decoded tree instead of re-parsing free text. ADS-C carries a precise fix,
        # so adopt it when the message had no other position.
        app = _decode_app(acars)
        if app is not None and app["lat"] is not None and lat is None:
            lat, lon = app["lat"], app["lon"]
        # Human-friendly breakout: category + extracted fields + a named label. These
        # ride through correlation onto event["acars"] and /api/acars unchanged, so the
        # GUI can display the pieces without re-parsing the raw text.
        category, fields = classify(label, text, flight, origin, destination, lat, lon)
        if app is not None:
            # The decoded application is the authoritative category; keep the identity
            # fields classify() surfaced (Flight/Route/Position) and append the app's.
            category = app["category"]
            fields = fields + app["fields"]
        return {
            "tail": tail,
            "flight_id": flight,
            "label": label,
            "label_name": _LABEL_NAMES.get(label, "Airline / other") if label else None,
            "category": category,
            "fields": fields,
            "text": text,
            "origin": origin,
            "destination": destination,
            "lat": lat,
            "lon": lon,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def drain_detections(self) -> list:
        """Atomically return and clear the buffered datalink messages."""
        out = self._buffer
        self._buffer = []
        return out
