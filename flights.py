#!/usr/bin/env python3
"""
Flight reconstruction for Hoboken Helo Accountability Tracker.

Reads recent observations from flights.db, groups them into flight segments
(gap > 5 min between consecutive observations of the same ICAO hex = new
flight), computes statistics and GeoJSON tracks, applies sanity checks and
policy flags, then upserts results into the flights table.

Safe to re-run: flight_id is deterministic (sha256 of icao_hex+started_at),
so INSERT OR REPLACE is idempotent.

Usage:
    python flights.py [--db-path PATH] [--data-dir PATH]
"""

import argparse
import hashlib
import json
import logging
import math
import sqlite3
import sys
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import init_db, commit_push

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

# ── Reference geography ───────────────────────────────────────────────────────

# Known heliports / airports in the corridor. FAA identifier → (lat, lon).
HELIPORTS = {
    "65NJ": (40.7480, -74.1043),  # HHI Kearny — primary target
    "JRB":  (40.7012, -74.0090),  # Downtown Manhattan / Wall Street
    "6N5":  (40.7427, -73.9719),  # East 34th St
    "JRA":  (40.7541, -74.0080),  # West 30th St
    "LDJ":  (40.6173, -74.2447),  # Linden Airport
}

# Hoboken city boundary polygon (clockwise, lat/lon pairs).
# Eastern edge follows the Hudson River waterfront, not a rectangle that
# extends into the river. Coordinates approximate the actual city limits.
HOBOKEN_POLYGON = [
    (40.7340, -74.0407),  # SW  Observer Hwy / 1st St west end
    (40.7590, -74.0380),  # NW  border with Weehawken (south of Weehawken)
    (40.7590, -74.0225),  # NE  north waterfront (Maxwell Place area)
    (40.7490, -74.0235),  # E   mid waterfront (Stevens Institute area)
    (40.7355, -74.0275),  # SE  south waterfront (Hoboken Terminal area)
]

# ── Timing / thresholds ───────────────────────────────────────────────────────

SEGMENT_GAP_SEC         = 300   # gap > 5 min → new flight segment
LOOKBACK_HOURS          = 6     # how far back to fetch observations
COMPLETE_AGE_SEC        = 0     # process all segments immediately; INSERT OR REPLACE keeps records idempotent
MIN_OBS_FOR_HIGH_CONF   = 2     # fewer obs → confidence=low
MAX_GAP_FOR_HIGH_CONF   = 180   # consecutive gap > 3 min → confidence=low
MAX_SPEED_FOR_HIGH_CONF = 250   # implied kt > 250 → confidence=low (spoofing)
HELIPORT_PROX_NM        = 0.3   # within 0.3nm → "at this heliport"
HOB_ALTITUDE_GATE_FT    = 3000  # min_alt above this → not a Hoboken overflight

# ── HHI permitted hours (PLACEHOLDER) ────────────────────────────────────────
#
# PLACEHOLDER: Replace with actual 2014 Kearny zoning approval text before
# filing any complaints based on this flag.
#
# These hours are approximate based on public community reporting:
#   Weekdays 07:00–19:00 ET, Weekends 09:00–17:00 ET
#
# The dashboard also labels this data as PLACEHOLDER. Do not treat these
# flags as definitive until confirmed against the official zoning document.

_ET = zoneinfo.ZoneInfo("America/New_York")


def outside_hhi_permitted_hours(dt_utc: datetime) -> bool:
    """
    PLACEHOLDER: returns True if dt_utc falls outside HHI's approximate
    permitted operating hours.
    """
    dt_et   = dt_utc.astimezone(_ET)
    hour    = dt_et.hour
    weekend = dt_et.weekday() >= 5   # Saturday=5, Sunday=6
    if weekend:
        return not (9 <= hour < 17)
    return not (7 <= hour < 19)

# ── Geometry helpers ──────────────────────────────────────────────────────────

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 3440.065  # Earth radius in nm
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def in_hoboken(lat: float, lon: float) -> bool:
    """Ray-casting point-in-polygon test against HOBOKEN_POLYGON."""
    # Fast bbox rejection before the polygon walk.
    if not (40.7340 <= lat <= 40.7590 and -74.0407 <= lon <= -74.0225):
        return False
    inside = False
    n = len(HOBOKEN_POLYGON)
    j = n - 1
    for i in range(n):
        xi, yi = HOBOKEN_POLYGON[i]  # lat, lon of vertex i
        xj, yj = HOBOKEN_POLYGON[j]  # lat, lon of vertex j
        if (yi > lon) != (yj > lon):
            cross_lat = xi + (lon - yi) / (yj - yi) * (xj - xi)
            if lat < cross_lat:
                inside = not inside
        j = i
    return inside


def _seg_intersects(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """True if segment AB properly crosses segment CD (ignores collinear)."""
    def _cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
    d1 = _cross(cx, cy, dx, dy, ax, ay)
    d2 = _cross(cx, cy, dx, dy, bx, by)
    d3 = _cross(ax, ay, bx, by, cx, cy)
    d4 = _cross(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def segment_crosses_hoboken(lat1: float, lon1: float,
                             lat2: float, lon2: float) -> bool:
    """
    True if segment (lat1,lon1)→(lat2,lon2) intersects HOBOKEN_POLYGON.

    Catches crossings when no individual observation falls inside the polygon —
    possible when an aircraft transits a corner between 10-second poll points.
    """
    if in_hoboken(lat1, lon1) or in_hoboken(lat2, lon2):
        return True
    n = len(HOBOKEN_POLYGON)
    for i in range(n):
        j = (i + 1) % n
        if _seg_intersects(lat1, lon1, lat2, lon2,
                           HOBOKEN_POLYGON[i][0], HOBOKEN_POLYGON[i][1],
                           HOBOKEN_POLYGON[j][0], HOBOKEN_POLYGON[j][1]):
            return True
    return False


def nearest_heliport(lat: float, lon: float) -> str | None:
    """Return FAA identifier of nearest heliport within HELIPORT_PROX_NM, or None."""
    best_id, best_dist = None, float("inf")
    for faa_id, (h_lat, h_lon) in HELIPORTS.items():
        d = haversine_nm(lat, lon, h_lat, h_lon)
        if d < best_dist:
            best_id, best_dist = faa_id, d
    return best_id if best_dist <= HELIPORT_PROX_NM else None

# ── N-number arithmetic fallback ──────────────────────────────────────────────

_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # 24 chars: A-Z minus I and O


def _icao_to_n_number(icao_hex: str) -> str | None:
    """
    Arithmetic fallback: derive N-number from US ICAO hex.
    Only covers N1–N1744 range. Prefer FAA database lookup (aircraft table).
    """
    try:
        v = int(icao_hex, 16)
    except ValueError:
        return None
    if not (0xA00001 <= v <= 0xAFFFFF):
        return None
    n    = v - 0xA00001
    slot = n // 601 + 1
    rem  = n % 601
    if slot > 99999:
        return None
    num = str(slot)
    if rem == 0:
        return f"N{num}"
    rem -= 1
    if rem < 24:
        return f"N{num}{_ALPHA[rem]}"
    rem -= 24
    first, second = divmod(rem, 24)
    if first >= 24:
        return None
    return f"N{num}{_ALPHA[first]}{_ALPHA[second]}"

# ── Operator loading ──────────────────────────────────────────────────────────

def _load_callsign_ops(path: Path) -> tuple[list, dict]:
    """Parse operators.yml without PyYAML.

    Returns (owner_name_terms, {UPPER_PREFIX: display_name}).
    """
    owner_names: list = []
    callsign_prefixes: dict = {}
    section = None
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line == 'owner_name:':
                section = 'owner_name'
            elif line == 'callsign_prefixes:':
                section = 'callsign_prefixes'
            elif section == 'owner_name' and line.startswith('- '):
                owner_names.append(line[2:].strip())
            elif section == 'callsign_prefixes' and ':' in line and not line.startswith('-'):
                prefix, rest = line.split(':', 1)
                prefix = prefix.strip()
                name = rest.split('#')[0].strip()
                if prefix and name:
                    callsign_prefixes[prefix.upper()] = name
    return owner_names, callsign_prefixes


# ── Flight ID and track helpers ───────────────────────────────────────────────

def make_flight_id(icao_hex: str, started_at: str) -> str:
    """Deterministic 16-char flight ID so re-runs are idempotent."""
    raw = f"{icao_hex}:{started_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_track(obs: list) -> str:
    """GeoJSON LineString (lon, lat order per GeoJSON spec)."""
    return json.dumps({
        "type": "LineString",
        "coordinates": [[o["lon"], o["lat"]] for o in obs],
    })


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# ── Segmentation ──────────────────────────────────────────────────────────────

def segment_observations(obs_list: list) -> list:
    """
    Split a time-sorted list of observations (same icao_hex) into flight
    segments wherever the gap between consecutive observations exceeds
    SEGMENT_GAP_SEC (5 min).
    """
    if not obs_list:
        return []
    segments, current = [], [obs_list[0]]
    for i in range(1, len(obs_list)):
        gap = (parse_iso(obs_list[i]["observed_at"])
               - parse_iso(obs_list[i - 1]["observed_at"])).total_seconds()
        if gap > SEGMENT_GAP_SEC:
            segments.append(current)
            current = [obs_list[i]]
        else:
            current.append(obs_list[i])
    segments.append(current)
    return segments

# ── Flight computation ────────────────────────────────────────────────────────

def compute_flight(obs: list, now_utc: datetime,
                   callsign_ops: dict | None = None) -> dict | None:
    """
    Compute all fields for a single flight segment.
    Returns None only if obs is empty.
    """
    if not obs:
        return None

    icao_hex   = obs[0]["icao_hex"]
    started_at = obs[0]["observed_at"]
    ended_at   = obs[-1]["observed_at"]

    # ── Sanity checks (any failure → confidence=low) ─────────────────────────
    confidence = "high"

    if len(obs) < MIN_OBS_FOR_HIGH_CONF:
        confidence = "low"
        log.debug("%s: too few observations (%d) → low confidence",
                  icao_hex, len(obs))

    for i in range(1, len(obs)):
        t1  = parse_iso(obs[i - 1]["observed_at"])
        t2  = parse_iso(obs[i]["observed_at"])
        gap = (t2 - t1).total_seconds()

        if gap > MAX_GAP_FOR_HIGH_CONF:
            confidence = "low"

        if gap > 0:
            dist_nm  = haversine_nm(obs[i - 1]["lat"], obs[i - 1]["lon"],
                                    obs[i]["lat"],     obs[i]["lon"])
            speed_kt = dist_nm / (gap / 3600)
            if speed_kt > MAX_SPEED_FOR_HIGH_CONF:
                confidence = "low"
                log.debug("%s: implied speed %.0fkt → low confidence",
                          icao_hex, speed_kt)

    # ── Altitude stats ────────────────────────────────────────────────────────
    alts     = [o["alt_baro_ft"] for o in obs if o["alt_baro_ft"] is not None]
    min_alt  = min(alts) if alts else None
    max_alt  = max(alts) if alts else None

    # ── Hoboken analysis ──────────────────────────────────────────────────────
    hob_obs  = [o for o in obs if in_hoboken(o["lat"], o["lon"])]
    crossed  = len(hob_obs) > 0

    # Interpolated crossing: check segments that bracket the polygon
    if not crossed:
        for i in range(1, len(obs)):
            if segment_crosses_hoboken(obs[i - 1]["lat"], obs[i - 1]["lon"],
                                       obs[i]["lat"],     obs[i]["lon"]):
                crossed = True
                break

    # Altitude gate: commercial jets crossing at cruise/approach altitude are
    # not Hoboken overflights. If every recorded altitude is above the gate,
    # clear the flag. Missing altitude data is treated as below the gate
    # (conservative — don't exclude aircraft that don't broadcast altitude).
    if crossed and min_alt is not None and min_alt > HOB_ALTITUDE_GATE_FT:
        crossed = False
        log.debug("%s: min_alt=%.0fft > gate → not a Hoboken overflight",
                  icao_hex, min_alt)

    # Min altitude uses only observations actually inside the polygon
    hob_alts         = [o["alt_baro_ft"] for o in hob_obs if o["alt_baro_ft"] is not None]
    min_alt_hoboken  = min(hob_alts) if hob_alts else None

    # Time in Hoboken: sum intervals where at least one endpoint is inside
    time_in_hoboken = None
    if hob_obs:
        time_in_hoboken = 0.0
        for i in range(1, len(obs)):
            p1_in = in_hoboken(obs[i - 1]["lat"], obs[i - 1]["lon"])
            p2_in = in_hoboken(obs[i]["lat"],     obs[i]["lon"])
            if p1_in or p2_in:
                gap = (parse_iso(obs[i]["observed_at"])
                       - parse_iso(obs[i - 1]["observed_at"])).total_seconds()
                if p1_in and p2_in:
                    time_in_hoboken += gap
                else:
                    time_in_hoboken += gap / 2  # one endpoint inside: rough midpoint

    # ── Heliport proximity ────────────────────────────────────────────────────
    dep_heliport = nearest_heliport(obs[0]["lat"],  obs[0]["lon"])
    arr_heliport = nearest_heliport(obs[-1]["lat"], obs[-1]["lon"])

    # ── Policy flags ──────────────────────────────────────────────────────────
    is_kearny    = dep_heliport == "65NJ"
    is_tour_op   = obs[0]["operator_flag"] is not None

    # PLACEHOLDER hours — see module-level comment above outside_hhi_permitted_hours()
    outside_hours = False
    if is_kearny:
        for o in obs:
            if outside_hhi_permitted_hours(parse_iso(o["observed_at"])):
                outside_hours = True
                break

    # ── N-number: FAA registry → ADS-B broadcast → arithmetic derivation ──────
    n_number = (
        next((o["n_number"]    for o in obs if o.get("n_number")),    None)
        or next((o["registration"] for o in obs if o.get("registration")), None)
        or _icao_to_n_number(icao_hex)
    )

    # ── Operator: callsign prefix (highest priority) → aircraft table ─────────
    # Callsign is broadcast by the actual flying entity, not the registered owner.
    # An aircraft leased from e.g. Meridian and operated by FlyNYON broadcasts
    # "NYON5" — matching the callsign prefix identifies the real operator.
    operator_flag = None
    if callsign_ops:
        for o in obs:
            cs = (o.get("callsign") or "").strip().upper()
            if cs:
                for prefix, op_name in callsign_ops.items():
                    if cs.startswith(prefix):
                        operator_flag = op_name
                        log.debug("%s: callsign %s → operator %s", icao_hex, cs, op_name)
                        break
            if operator_flag:
                break
    if operator_flag is None:
        operator_flag = next((o["operator_flag"] for o in obs if o.get("operator_flag")), None)

    return {
        "flight_id":               make_flight_id(icao_hex, started_at),
        "icao_hex":                icao_hex,
        "n_number":                n_number,
        "operator_flag":           operator_flag,
        "started_at":              started_at,
        "ended_at":                ended_at,
        "departure_heliport":      dep_heliport,
        "arrival_heliport":        arr_heliport,
        "min_alt_baro_ft":         min_alt,
        "max_alt_baro_ft":         max_alt,
        "crossed_hoboken":         1 if crossed else 0,
        "min_alt_over_hoboken_ft": min_alt_hoboken,
        "time_in_hoboken_sec":     time_in_hoboken,
        "total_observations":      len(obs),
        "confidence":              confidence,
        "track_summary":           make_track(obs),
        "is_kearny_departure":     1 if is_kearny else 0,
        "outside_hhi_hours":       1 if outside_hours else 0,
        "is_tour_operator":        1 if is_tour_op else 0,
        "reconstructed_at":        iso(now_utc),
    }

# ── Upsert ────────────────────────────────────────────────────────────────────

_UPSERT_FLIGHT = """\
INSERT OR REPLACE INTO flights (
    flight_id, icao_hex, n_number, operator_flag,
    started_at, ended_at, departure_heliport, arrival_heliport,
    min_alt_baro_ft, max_alt_baro_ft,
    crossed_hoboken, min_alt_over_hoboken_ft, time_in_hoboken_sec,
    total_observations, confidence, track_summary,
    is_kearny_departure, outside_hhi_hours, is_tour_operator,
    reconstructed_at
) VALUES (
    :flight_id, :icao_hex, :n_number, :operator_flag,
    :started_at, :ended_at, :departure_heliport, :arrival_heliport,
    :min_alt_baro_ft, :max_alt_baro_ft,
    :crossed_hoboken, :min_alt_over_hoboken_ft, :time_in_hoboken_sec,
    :total_observations, :confidence, :track_summary,
    :is_kearny_departure, :outside_hhi_hours, :is_tour_operator,
    :reconstructed_at
)
"""

# ── Main reconstruction loop ──────────────────────────────────────────────────

_FETCH_OBS = """\
SELECT
    o.icao_hex, o.lat, o.lon, o.alt_baro_ft,
    o.ground_speed_kt, o.observed_at, o.registration, o.callsign,
    a.n_number, a.operator_flag
FROM observations o
LEFT JOIN aircraft a ON o.icao_hex = a.icao_hex
WHERE o.observed_at >= ?
ORDER BY o.icao_hex, o.observed_at
"""

_COLS = ("icao_hex", "lat", "lon", "alt_baro_ft",
         "ground_speed_kt", "observed_at", "registration", "callsign",
         "n_number", "operator_flag")


def reconstruct_all(conn: sqlite3.Connection, now_utc: datetime,
                    callsign_ops: dict | None = None) -> tuple:
    """Reconstruct all complete flight segments in the lookback window."""
    cutoff   = now_utc - timedelta(hours=LOOKBACK_HOURS)
    rows     = conn.execute(_FETCH_OBS, (iso(cutoff),)).fetchall()
    obs_list = [dict(zip(_COLS, r)) for r in rows]

    # Group by icao_hex (already sorted by (icao_hex, observed_at) from SQL)
    groups: dict[str, list] = {}
    for o in obs_list:
        groups.setdefault(o["icao_hex"], []).append(o)

    n_processed = n_skipped = 0
    flights_upserted = []

    for icao_hex, obs in groups.items():
        for seg in segment_observations(obs):
            last_t = parse_iso(seg[-1]["observed_at"])
            age    = (now_utc - last_t).total_seconds()
            if age < COMPLETE_AGE_SEC:
                n_skipped += 1
                continue   # flight might still be active — wait for next run

            flight = compute_flight(seg, now_utc, callsign_ops)
            if flight:
                conn.execute(_UPSERT_FLIGHT, flight)
                flights_upserted.append(flight)
                n_processed += 1

    conn.commit()
    return n_processed, n_skipped


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Hoboken Helo Accountability — flight reconstruction")
    p.add_argument("--db-path",  type=Path, default=Path("data-branch/flights.db"))
    p.add_argument("--data-dir", type=Path, default=Path("data-branch"))
    args = p.parse_args()

    now_utc = datetime.now(timezone.utc)
    conn    = init_db(args.db_path)

    # Load callsign-prefix → operator mappings from operators.yml if present.
    callsign_ops: dict = {}
    ops_path = Path(__file__).with_name("operators.yml")
    if ops_path.exists():
        _, callsign_ops = _load_callsign_ops(ops_path)
        log.info("Loaded %d callsign prefix(es) from operators.yml", len(callsign_ops))

    n_proc, n_skip = reconstruct_all(conn, now_utc, callsign_ops)
    conn.close()

    log.info("Reconstruction complete: %d flights upserted, %d segments still active",
             n_proc, n_skip)

    ts = iso(now_utc)
    commit_push(args.data_dir, f"reconstruct: {ts} ({n_proc} flights)", args.db_path)


if __name__ == "__main__":
    main()
