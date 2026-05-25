#!/usr/bin/env python3
"""
ADS-B observation harvester for Hoboken Helo Accountability Tracker.

Polls adsb.fi (primary) every 10 seconds. After 3 consecutive failures,
falls back to adsb.lol for 5 minutes before retrying the primary. Writes
validated observations to SQLite and commits to the data branch every 5 min.

Usage:
    python harvester.py [--duration N] [--db-path PATH] [--data-dir PATH]
"""

import argparse
import json
import logging
import re
import signal
import sqlite3
import sys
import time
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

import requests

from db import SCHEMA_SQL, init_db, git, commit_push  # noqa: F401 (SCHEMA_SQL re-exported)

# ── Identity ──────────────────────────────────────────────────────────────────

VERSION  = "0.1"
REPO_URL = "https://github.com/chmavo/hudson-helo-tracker"
USER_AGENT = f"hoboken-helo-accountability/{VERSION} (+{REPO_URL})"

# ── Endpoints ─────────────────────────────────────────────────────────────────

PRIMARY_URL  = "https://opendata.adsb.fi/api/v3/lat/40.7440/lon/-74.0324/dist/2"
FALLBACK_URL = "https://api.adsb.lol/v2/point/40.7440/-74.0324/2"

# ── Timing ────────────────────────────────────────────────────────────────────

POLL_INTERVAL_ACTIVE_SEC    = 10   # 6 AM – 11 PM ET
POLL_INTERVAL_OVERNIGHT_SEC = 20   # 11 PM – 6 AM ET
ACTIVE_HOUR_START_ET        = 6    # 6 AM ET — switch to active cadence
ACTIVE_HOUR_END_ET          = 23   # 11 PM ET — switch to overnight cadence
COMMIT_INTERVAL_SEC         = 300  # 5 minutes
FALLBACK_DURATION_SEC       = 300  # stay on fallback before retrying primary
FAILURES_BEFORE_FALLBACK    = 3
REQUEST_TIMEOUT_SEC         = 8
MAX_BACKOFF_SEC             = 120

_ET = zoneinfo.ZoneInfo("America/New_York")

# ── Validation bounds ─────────────────────────────────────────────────────────

LAT_MIN, LAT_MAX        =  40.71,  40.78
LON_MIN, LON_MAX        = -74.08, -73.99
ALT_MIN_FT, ALT_MAX_FT  = -500,  15000
SPD_MIN_KT, SPD_MAX_KT  =    0,    300
API_SKEW_TOLERANCE_SEC  =  300   # reject batch if API clock drifts >5 min

# ADS-B category A7 = helicopter/gyroplane per ICAO Annex 10.
# Operators in this corridor (tour operators, corporate, news) use modern
# avionics that broadcast A7. Accepting unknown category would re-admit
# the bulk of non-reporting fixed-wing traffic in the Newark/Teterboro area.
ROTORCRAFT_CATEGORIES = {"A7"}

ICAO_RE = re.compile(r'^[0-9A-F]{6}$')

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Signal ────────────────────────────────────────────────────────────────────

_shutdown              = False
_last_logged_interval: int | None = None

def _handle_sigterm(signum, frame):
    global _shutdown
    log.info("SIGTERM received — will exit after current poll.")
    _shutdown = True

# ── Utilities ─────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def round5s(dt: datetime) -> str:
    """Round to nearest 5 seconds, return UTC ISO8601 string."""
    ts = dt.timestamp()
    rounded = round(ts / 5) * 5
    return datetime.fromtimestamp(rounded, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def current_poll_interval(now_utc: datetime) -> int:
    """Return poll interval in seconds based on Eastern Time hour.

    Active (10s): 6 AM – 10:59 PM ET  (ACTIVE_HOUR_START_ET <= hour < ACTIVE_HOUR_END_ET)
    Overnight (20s): 11 PM – 5:59 AM ET
    """
    et_hour = now_utc.astimezone(_ET).hour
    if ACTIVE_HOUR_START_ET <= et_hour < ACTIVE_HOUR_END_ET:
        return POLL_INTERVAL_ACTIVE_SEC
    return POLL_INTERVAL_OVERNIGHT_SEC

# ── Validation ────────────────────────────────────────────────────────────────

def validate(ac: dict, source: str, observed_at: str) -> tuple:
    """
    Normalize and validate one aircraft record from the API.
    Returns (row_dict, None) on success or (None, reason_str) on failure.
    """
    raw_hex = str(ac.get("hex", "")).strip().lstrip("~").upper()
    if not ICAO_RE.match(raw_hex):
        return None, f"bad icao_hex={raw_hex!r}"

    category = ac.get("category") or None
    if category not in ROTORCRAFT_CATEGORIES:
        return None, f"category={category!r} not rotorcraft"

    try:
        lat = float(ac["lat"])
        lon = float(ac["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"bad lat/lon: {exc}"

    if not (LAT_MIN <= lat <= LAT_MAX):
        return None, f"lat={lat} outside [{LAT_MIN}, {LAT_MAX}]"
    if not (LON_MIN <= lon <= LON_MAX):
        return None, f"lon={lon} outside [{LON_MIN}, {LON_MAX}]"

    on_ground = 0
    alt_baro_ft = None
    raw_alt = ac.get("alt_baro")
    if raw_alt == "ground":
        alt_baro_ft = 0.0
        on_ground = 1
    elif raw_alt is not None:
        try:
            alt_baro_ft = float(raw_alt)
        except (TypeError, ValueError):
            return None, f"bad alt_baro={raw_alt!r}"
        if not (ALT_MIN_FT <= alt_baro_ft <= ALT_MAX_FT):
            return None, f"alt_baro_ft={alt_baro_ft} outside [{ALT_MIN_FT}, {ALT_MAX_FT}]"

    ground_speed_kt = None
    raw_gs = ac.get("gs")
    if raw_gs is not None:
        try:
            ground_speed_kt = float(raw_gs)
        except (TypeError, ValueError):
            return None, f"bad gs={raw_gs!r}"
        if not (SPD_MIN_KT <= ground_speed_kt <= SPD_MAX_KT):
            return None, f"gs={ground_speed_kt} outside [{SPD_MIN_KT}, {SPD_MAX_KT}]"

    def _f(key):
        v = ac.get(key)
        return float(v) if v is not None else None

    return {
        "icao_hex":          raw_hex,
        "callsign":          (ac.get("flight") or ac.get("callsign") or "").strip() or None,
        "registration":      (ac.get("r") or ac.get("reg") or "").strip() or None,
        "aircraft_type":     (ac.get("t") or ac.get("type") or "").strip() or None,
        "category":          ac.get("category"),
        "lat":               lat,
        "lon":               lon,
        "alt_baro_ft":       alt_baro_ft,
        "alt_geom_ft":       _f("alt_geom"),
        "ground_speed_kt":   ground_speed_kt,
        "track_deg":         _f("track"),
        "vertical_rate_fpm": _f("baro_rate"),
        "on_ground":         on_ground,
        "observed_at":       observed_at,
        "source_api":        source,
    }, None

# ── Database ──────────────────────────────────────────────────────────────────

_INSERT_OBS = """\
INSERT OR IGNORE INTO observations
    (icao_hex, callsign, registration, aircraft_type, category,
     lat, lon, alt_baro_ft, alt_geom_ft, ground_speed_kt,
     track_deg, vertical_rate_fpm, on_ground, observed_at, source_api)
VALUES
    (:icao_hex, :callsign, :registration, :aircraft_type, :category,
     :lat, :lon, :alt_baro_ft, :alt_geom_ft, :ground_speed_kt,
     :track_deg, :vertical_rate_fpm, :on_ground, :observed_at, :source_api)
"""

_INSERT_REJ = """\
INSERT INTO rejected_observations (raw_json, rejection_reason, observed_at, source_api)
VALUES (?, ?, ?, ?)
"""

# ── Main loop ─────────────────────────────────────────────────────────────────

def run(duration_sec: int, db_path: Path, data_dir: Path) -> None:
    global _shutdown, _last_logged_interval
    signal.signal(signal.SIGTERM, _handle_sigterm)

    conn = init_db(db_path)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    loop_start   = time.monotonic()
    next_commit  = time.monotonic() + COMMIT_INTERVAL_SEC
    obs_window   = 0

    consecutive_failures = 0
    using_fallback       = False
    fallback_until       = 0.0
    backoff              = 10.0

    log.info("Harvester started — duration=%ds db=%s", duration_sec, db_path)

    while True:
        poll_start    = time.monotonic()
        poll_interval = current_poll_interval(utcnow())
        if poll_interval != _last_logged_interval:
            now_u = utcnow()
            label = "active" if poll_interval == POLL_INTERVAL_ACTIVE_SEC else "overnight"
            log.info("Switched to %s cadence (%ds) at %s UTC / %s ET",
                     label, poll_interval,
                     now_u.strftime("%Y-%m-%d %H:%M:%S"),
                     now_u.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S"))
            _last_logged_interval = poll_interval

        # ── Exit conditions ──────────────────────────────────────────────────
        if _shutdown or (poll_start - loop_start) >= duration_sec:
            log.info("Exiting — %s", "SIGTERM" if _shutdown else "duration elapsed")
            break

        # ── Source selection ─────────────────────────────────────────────────
        if using_fallback and time.time() >= fallback_until:
            using_fallback = False
            log.info("Fallback period expired — resuming adsb.fi primary")
        url    = FALLBACK_URL if using_fallback else PRIMARY_URL
        source = "adsb.lol"  if using_fallback else "adsb.fi"

        # ── Fetch ────────────────────────────────────────────────────────────
        aircraft = None
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SEC)
            if resp.status_code == 429:
                log.warning("429 from %s — backing off %ds", source, int(backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                continue
            resp.raise_for_status()
            payload = resp.json()
            aircraft = payload.get("ac", [])

            # Drop the batch if the API's reported timestamp is severely skewed.
            # adsb.fi returns 'now' in milliseconds; values >1e10 are ms, not s.
            api_now = payload.get("now")
            if api_now is not None:
                api_ts = float(api_now)
                if api_ts > 1e10:
                    api_ts /= 1000.0
                skew = abs(time.time() - api_ts)
                if skew > API_SKEW_TOLERANCE_SEC:
                    log.warning("API clock skew=%.0fs from %s — dropping batch", skew, source)
                    aircraft = []

            consecutive_failures = 0
            backoff = 10.0
        except Exception as exc:
            consecutive_failures += 1
            log.warning("Fetch error #%d from %s: %s",
                        consecutive_failures, source, exc)
            if not using_fallback and consecutive_failures >= FAILURES_BEFORE_FALLBACK:
                log.warning("3 consecutive failures — switching to adsb.lol for 5 min")
                using_fallback    = True
                fallback_until    = time.time() + FALLBACK_DURATION_SEC
                consecutive_failures = 0
            time.sleep(max(0.0, poll_interval - (time.monotonic() - poll_start)))
            continue

        # ── Validate and insert ──────────────────────────────────────────────
        now_str   = round5s(utcnow())
        new_obs   = 0
        rejected  = 0

        try:
            with conn:
                for ac in aircraft:
                    row, reason = validate(ac, source, now_str)
                    if reason:
                        conn.execute(_INSERT_REJ,
                                     (json.dumps(ac), reason, now_str, source))
                        rejected += 1
                    else:
                        cur = conn.execute(_INSERT_OBS, row)
                        new_obs += cur.rowcount
        except sqlite3.Error as exc:
            log.error("DB error: %s", exc)

        obs_window += new_obs
        log.info("poll ok | %d aircraft | %d new | %d rejected | %s",
                 len(aircraft), new_obs, rejected, source)

        # ── Commit if due ────────────────────────────────────────────────────
        if time.monotonic() >= next_commit:
            commit_push(data_dir, f"harvest: {iso(utcnow())} ({obs_window} new obs)", db_path)
            obs_window  = 0
            next_commit = time.monotonic() + COMMIT_INTERVAL_SEC

        # ── Sleep to maintain cadence ─────────────────────────────────────────
        time.sleep(max(0.0, poll_interval - (time.monotonic() - poll_start)))

    # ── Shutdown: final commit ────────────────────────────────────────────────
    commit_push(data_dir, f"harvest: final {iso(utcnow())} ({obs_window} new obs)", db_path)
    conn.close()
    log.info("Harvester exited cleanly.")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Hoboken Helo Accountability — ADS-B harvester")
    p.add_argument("--duration", type=int, default=21000,
                   help="Run duration in seconds (default 21000 = 5h50m)")
    p.add_argument("--db-path",  type=Path, default=Path("data-branch/flights.db"),
                   help="Path to SQLite flights.db")
    p.add_argument("--data-dir", type=Path, default=Path("data-branch"),
                   help="Root of the git-managed data directory")
    args = p.parse_args()
    run(args.duration, args.db_path, args.data_dir)

if __name__ == "__main__":
    # ── Cadence unit tests ────────────────────────────────────────────────────
    def _et(h: int, m: int = 0, s: int = 0) -> datetime:
        return datetime(2026, 5, 26, h, m, s,
                        tzinfo=zoneinfo.ZoneInfo("America/New_York")
                        ).astimezone(timezone.utc)

    assert current_poll_interval(_et(14))         == POLL_INTERVAL_ACTIVE_SEC,    "2 PM ET → 10s"
    assert current_poll_interval(_et(2))           == POLL_INTERVAL_OVERNIGHT_SEC, "2 AM ET → 20s"
    assert current_poll_interval(_et(23, 0, 0))   == POLL_INTERVAL_OVERNIGHT_SEC, "11 PM ET exactly → 20s"
    assert current_poll_interval(_et(6, 0, 0))    == POLL_INTERVAL_ACTIVE_SEC,    "6 AM ET exactly → 10s"
    print("Cadence tests passed.")

    main()
