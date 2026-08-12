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
# "POSN51500W000100" = 51.500 N, 0.100 W (lat DD + 3 fractional digits, lon DDD + 3).
# NOT degree-minutes — verified against real reports (a "…W000678" → 0.678, which as
# minutes would be an impossible 67.8'). An optional 2-char report subtype may
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
        # Comma-separated degree-minute fix as written in a "++865xx" track row
        # ("N5130.4,W00007.6") — the first waypoint locates the message.
        fix = _degmin_comma(text)
        if fix:
            return fix
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

# Classification-schema version. Stamped onto every classified record (``cver``) and
# used by reclassify() to decide whether a stored record's breakout is current. BUMP
# this whenever classify()/the structured-report parsers change what they emit, so the
# GUI re-decodes already-classified history at serve time instead of showing the stale
# breakout (records carry the category the OLD code baked in, so a plain "has a
# category" check would skip them forever). v2 = engine/position report families.
_CLASSIFY_VERSION = 2

# Labels that are pure link/media management (no user-facing content).
_LINK_LABELS = {"_d", "_j", "sa", "q0", "sm", "sv"}
# Engine / performance report markers seen in the free text (e.g. an "APM" report or
# an ACMF snapshot). Word-boundaried so they don't fire on noise.
_PERF_RE = re.compile(r"\b(APM|ACMF|PERF|ENGINE|FUEL FLOW)\b", re.I)
# OOOI (Out/Off/On/In) event paired with a clock time; we require two or more pairs so
# an incidental "ON"/"IN" in free text can't be mistaken for a flight-progress report.
_OOOI_PAIR_RE = re.compile(r"\b(OUT|OFF|ON|IN)\s*(\d{4})\b", re.I)


def _fmt_pos(lat, lon) -> str:
    """A position as a compact hemisphere string, e.g. 'N51.500, W000.100'."""
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


# ---------------------------------------------------------------------------
# Structured airline report families (header + self-describing fields)
# ---------------------------------------------------------------------------
#
# A large share of what lands in "Free text / other" is not free text at all —
# it is an airline engine/position report in one of a few recurring dialects
# (APM, the "<701>" downlink, the "++865xx" position track, and the "#"-delimited
# CRUISE report). Their DEEP columns (per-engine EGT / fuel flow / N2 / oil) are
# proprietary and positionally undocumented, so we do NOT decode those. But every
# one of these carries a stable HEADER — registration, flight/callsign, route,
# aircraft type, report time — that today is thrown away, plus a few values that
# are self-describing (Mach, altitude, outside-air temperature, N1 fan speed). We
# surface exactly those and leave the rest in the raw text: same "name what we can,
# never fake-decode" stance the rest of the module follows. Every extractor is
# defensive — a field is emitted only when its token matches the expected shape, so
# an unforeseen airline variant degrades to fewer fields rather than a wrong value.


def _mach(tok: str) -> Optional[str]:
    """A cruise-Mach token (``.850`` / ``0.804``) → ``'0.850'``; else None. Ranged so a
    stray fractional (a latitude's decimals) can't be mistaken for a Mach number."""
    m = re.fullmatch(r"0?\.(\d{3})", tok.strip())
    if not m:
        return None
    v = int(m.group(1)) / 1000.0
    return f"{v:.3f}" if 0.40 <= v <= 0.98 else None


def _alt_ft(tok: str) -> Optional[int]:
    """A cruise-altitude token (feet) → int, if it falls in a plausible cruise band."""
    t = tok.strip()
    if not re.fullmatch(r"\d{4,6}", t):
        return None
    v = int(t)
    return v if 8000 <= v <= 45000 else None


def _sat(tok: str) -> Optional[str]:
    """An outside-air / static-air temperature token (°C) → clean string, if in range.
    Cruise SAT is deeply negative; we allow down to +60 for low-level reports."""
    t = tok.strip()
    if not re.fullmatch(r"-?\d{1,2}\.\d", t):
        return None
    v = float(t)
    return f"{v:.1f}" if -90.0 <= v <= 60.0 else None


def _pct(tok: str) -> Optional[float]:
    """An engine rotor-speed percent token (N1/N2) → float, if 0–115 %."""
    try:
        v = float(tok.strip())
    except (TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 115.0 else None


def _is_icao(s) -> bool:
    return isinstance(s, str) and len(s) == 4 and s.isalpha() and s.isupper()


def _hhmmss_clock(tok: str) -> Optional[str]:
    """A 6-digit HHMMSS report time → ``'HH:MM:SS'``, only if it is a real clock time."""
    t = tok.strip()
    if not re.fullmatch(r"\d{6}", t):
        return None
    hh, mm, ss = int(t[0:2]), int(t[2:4]), int(t[4:6])
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh < 24 and mm < 60 and ss < 60 else None


def _fl(alt_ft: int) -> str:
    """Feet → a flight level label, e.g. 34001 → 'FL340'."""
    return f"FL{round(alt_ft / 100):03d}"


# APM (Aircraft Performance Monitoring, Teledyne ACMS) header. The report id/counter
# between "APM" and the tail is optional; the route is a concatenated ICAO origin+dest
# pair, and the trailer is DDMMYY + HHMMSS. Deliberately strict so it only fires on a
# real APM header line.
_APM_HEADER_RE = re.compile(
    r"\bAPM\b\s*\d*\s+(?P<reg>[A-Z]\d?[A-Z0-9]{3,5})\s+(?P<flight>[A-Z]{2,3}\d{1,4})\s+"
    r"(?P<orig>[A-Z]{4})(?P<dest>[A-Z]{4})\d{6}(?P<time>\d{6})"
)
# "<701>" engine downlink: the identity is a concatenated tail+callsign, and the route
# is the only 8-letter run bounded by digits on both sides — anchoring on those digits
# keeps it from matching an unrelated word (e.g. a trailing "ENGD330NOTA").
_S701_ID_RE = re.compile(r"\b(?P<reg>N[A-Z0-9]{4,5})(?P<flight>[A-Z]{2,3}\d{2,4})\b")
_S701_ROUTE_RE = re.compile(r"(?<=\d)(?P<orig>[A-Z]{4})(?P<dest>[A-Z]{4})(?=\d)")
# A degree-minute fix as written in a comma-delimited track row: "N5130.4,W00007.6".
_DEGMIN_COMMA_RE = re.compile(
    r"\b(?P<lath>[NS])(?P<latd>\d{2})(?P<latm>\d{2}\.\d+)\s*,\s*"
    r"(?P<lonh>[EW])(?P<lond>\d{3})(?P<lonm>\d{2}\.\d+)"
)


def _degmin_comma(text: str):
    """First comma-separated degree-minute fix in *text* → ``(lat, lon)`` or None."""
    m = _DEGMIN_COMMA_RE.search(text)
    if not m:
        return None
    lat = (int(m.group("latd")) + float(m.group("latm")) / 60.0) * (
        1 if m.group("lath") == "N" else -1)
    lon = (int(m.group("lond")) + float(m.group("lonm")) / 60.0) * (
        1 if m.group("lonh") == "E" else -1)
    if abs(lat) <= 90 and abs(lon) <= 180:
        return lat, lon
    return None


def _parse_apm(text: str) -> Optional[dict]:
    """APM engine-performance report → header fields + the (unambiguous) cruise Mach.
    Deep engine columns are airline-proprietary and left in the raw text."""
    m = _APM_HEADER_RE.search(text)
    if not m:
        return None
    fields = [{"name": "Report", "value": "Engine performance (APM)"},
              {"name": "Registration", "value": m.group("reg")},
              {"name": "Flight", "value": m.group("flight")},
              {"name": "Route", "value": f'{m.group("orig")}→{m.group("dest")}'}]
    clock = _hhmmss_clock(m.group("time"))
    if clock:
        fields.append({"name": "Report time", "value": f"{clock}Z"})
    # Mach is the first cruise-range decimal token after the header (config/route ids
    # carry no decimal point; the lat/lon decimals appear only later in the payload).
    for tok in re.findall(r"0?\.\d{3}", text[m.end():]):
        mach = _mach(tok)
        if mach:
            fields.append({"name": "Mach", "value": mach})
            break
    return {"category": CATEGORY_PERFORMANCE, "fields": fields, "lat": None, "lon": None}


def _parse_701(text: str) -> Optional[dict]:
    """"<701>" engine downlink → header fields (identity + route). The numeric body is
    an undocumented per-airline engine snapshot; it stays in the raw text."""
    if "<701>" not in text:
        return None
    fields = [{"name": "Report", "value": "Engine data (<701>)"}]
    ident = _S701_ID_RE.search(text)
    if ident:
        fields.append({"name": "Registration", "value": ident.group("reg")})
        fields.append({"name": "Flight", "value": ident.group("flight")})
    route = _S701_ROUTE_RE.search(text)
    if route:
        fields.append({"name": "Route", "value": f'{route.group("orig")}→{route.group("dest")}'})
    return {"category": CATEGORY_PERFORMANCE, "fields": fields, "lat": None, "lon": None}


def _parse_track(text: str) -> Optional[dict]:
    """"++865xx" position/track report → header (identity/type/route) plus a summary of
    the waypoint track. Cleanly comma-delimited, so every field is shape-validated and
    the first fix is adopted as the message position."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("++"):
        return None
    head = [t.strip() for t in lines[0].split(",")]
    fields = [{"name": "Report", "value": "Position / track report"}]
    reg = head[1] if len(head) > 1 else ""
    typ = head[2] if len(head) > 2 else ""
    flight = head[4] if len(head) > 4 else ""
    orig = head[5] if len(head) > 5 else ""
    dest = head[6] if len(head) > 6 else ""
    if re.fullmatch(r"N?[A-Z0-9]{4,6}", reg):
        fields.append({"name": "Registration", "value": reg})
    if re.fullmatch(r"[A-Z][A-Z0-9]{2,7}", typ):
        fields.append({"name": "Aircraft type", "value": typ})
    if re.fullmatch(r"[A-Z]{2,3}\d{1,4}", flight):
        fields.append({"name": "Flight", "value": flight})
    if _is_icao(orig) and _is_icao(dest):
        fields.append({"name": "Route", "value": f"{orig}→{dest}"})
    # Waypoint rows: "<degmin lat>,<degmin lon>,<HHMMSS>,<alt ft>,...". Count them and
    # take the first fix; report the altitude band when the alt column validates.
    first = None
    count = 0
    alts = []
    for ln in lines[1:]:
        fix = _degmin_comma(ln)
        if not fix:
            continue
        count += 1
        if first is None:
            first = fix
        cells = [c.strip() for c in ln.split(",")]
        if len(cells) >= 4:
            a = _alt_ft(cells[3])
            if a:
                alts.append(a)
    lat = lon = None
    if first is not None:
        lat, lon = first
        fields.append({"name": "Fixes", "value": f"{count} position{'s' if count != 1 else ''}"})
        if alts:
            lo, hi = min(alts), max(alts)
            fields.append({"name": "Altitude",
                           "value": _fl(hi) if lo == hi else f"{_fl(lo)}–{_fl(hi)}"})
    return {"category": CATEGORY_POSITION, "fields": fields, "lat": lat, "lon": lon}


def _parse_cruise(text: str) -> Optional[dict]:
    """"CRUISE REPORT" ("#"-delimited) → cruise state (altitude / SAT / Mach) and the
    per-engine N1/N2. The "#" delimiting makes these columns unambiguous, so they are
    safe to name; remaining columns (fuel flow / EGT / oil) stay in the raw text."""
    if "CRUISE" not in text[:40].upper():
        return None
    lines = text.splitlines()
    fields = [{"name": "Report", "value": "Engine cruise report"}]
    # Header line: locate the "CRUISE" marker, then the first consecutive
    # (altitude, SAT, Mach) triple that all validate.
    head_cells = None
    for ln in lines:
        cells = [c for c in ln.split("#") if c != ""]
        if any(c.upper() == "CRUISE" for c in cells):
            head_cells = cells
            break
    if head_cells:
        for i in range(len(head_cells) - 2):
            alt = _alt_ft(head_cells[i])
            sat = _sat(head_cells[i + 1])
            mach = _mach(head_cells[i + 2])
            if alt is not None and sat is not None and mach is not None:
                fields.append({"name": "Altitude", "value": _fl(alt)})
                fields.append({"name": "OAT", "value": f"{sat}°C"})
                fields.append({"name": "Mach", "value": mach})
                break
    # Engine lines: an all-"#" numeric row whose first two cells are rotor-speed
    # percents. The header row (leading report id > 115) and the trailing ratio row
    # (leading value < 40) are naturally excluded by the percent range.
    eng = 0
    for ln in lines:
        cells = [c for c in ln.split("#") if c != ""]
        if len(cells) < 2:
            continue
        n1, n2 = _pct(cells[0]), _pct(cells[1])
        if n1 is not None and n2 is not None and 40.0 <= n1 <= 105.0 and 40.0 <= n2 <= 105.0:
            eng += 1
            fields.append({"name": f"Eng {eng} N1/N2", "value": f"{n1:.1f}% / {n2:.1f}%"})
        if eng >= 4:
            break
    return {"category": CATEGORY_PERFORMANCE, "fields": fields, "lat": None, "lon": None}


def _parse_structured_report(text: Optional[str]) -> Optional[dict]:
    """If *text* is a recognized airline engine/position report, return
    ``{"category", "fields", "lat", "lon"}`` with the safely-readable fields; else None.
    Guarded so a malformed real-world variant never raises into the caller."""
    if not text:
        return None
    for parser in (_parse_apm, _parse_701, _parse_track, _parse_cruise):
        try:
            result = parser(text)
        except Exception:  # pragma: no cover - defensive on live decoder output
            logger.debug("ACARS structured-report parse failed", exc_info=True)
            continue
        if result and len(result["fields"]) > 1:  # more than just the "Report" label
            return result
    return None


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

    # A recognized airline engine/position report is the most specific signal and wins
    # outright: name its type and surface the header + self-describing values. The
    # report-type label leads, then the identity we already have, then the report's own
    # fields — de-duplicated by name so the cleaner base identity wins on any conflict.
    structured = _parse_structured_report(txt)
    if structured is not None:
        extras = structured["fields"]
        seen, merged = set(), []
        for f in extras[:1] + fields + extras[1:]:
            if f["name"] in seen:
                continue
            seen.add(f["name"])
            merged.append(f)
        return structured["category"], merged

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
    stored ACARS record, derived from its own raw fields, so historical messages show
    the same breakout as freshly-decoded ones. Used at GUI serve time.

    Returns a shallow copy so the caller's cached/stored dict is never mutated. Skips
    (returns unchanged) only a record already classified by the *current* schema
    (``cver == _CLASSIFY_VERSION``); a record carrying an OLDER classification — including
    one the previous code baked a category into — is re-decoded so a schema improvement
    reaches history without a log rewrite.
    """
    if not isinstance(rec, dict) or rec.get("cver") == _CLASSIFY_VERSION:
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
    out["cver"] = _CLASSIFY_VERSION
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
        # Guarded: a decode failure on an unforeseen real-world structure must never
        # drop the message or break the UDP callback — fall back to text classification.
        try:
            app = _decode_app(acars)
        except Exception:  # pragma: no cover - defensive on live decoder output
            logger.debug("ACARS app-layer decode failed; using text classification",
                         exc_info=True)
            app = None
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
            "cver": _CLASSIFY_VERSION,   # freshly decoded → reclassify() leaves it as-is
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def drain_detections(self) -> list:
        """Atomically return and clear the buffered datalink messages."""
        out = self._buffer
        self._buffer = []
        return out
