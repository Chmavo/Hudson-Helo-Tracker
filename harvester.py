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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Identity ──────────────────────────────────────────────────────────────────

VERSION  = "0.1"
REPO_URL = "https://github.com/chmavo/hudson-helo-tracker"
USER_AGENT = f"hoboken-helo-accountability/{VERSION} (+{REPO_URL})"

# ── Endpoints ─────────────────────────────────────────────────────────────────

PRIMARY_URL  = "https://opendata.adsb.fi/api/v3/lat/40.7440/lon/-74.0324/dist/8"
FALLBACK_URL = "https://api.adsb.lol/v2/point/40.7440/-74.0324/8"

# ── Timing ────────────────────────────────────────────────────────────────────

POLL_INTERVAL_SEC        = 10
COMMIT_INTERVAL_SEC      = 300   # 5 minutes
FALLBACK_DURATION_SEC    = 300   # stay on fallback before retrying primary
FAILURES_BEFORE_FALLBACK = 3
REQUEST_TIMEOUT_SEC      = 8
MAX_BACKOFF_SEC          = 120

# ── Validation bounds ─────────────────────────────────────────────────────────

LAT_MIN, LAT_MAX        =  40.5,   41.0
LON_MIN, LON_MAX        = -74.3,  -73.8
ALT_MIN_FT, ALT_MAX_FT  = -500,  15000
SPD_MIN_KT, SPD_MAX_KT  =    0,    300
API_SKEW_TOLERANCE_SEC  =  300   # reject batch if API clock drifts >5 min

ICAO_RE = re.compile(r'^[0-9A-F]{6}$')

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Full schema (all stages) ──────────────────────────────────────────────────
#
# Defined here so every script can call init_db() idempotently without
# migrations. Only observations + rejected_observations are written by
# this module; aircraft / flights / submissions are for Stages 2-5.

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY,
    icao_hex          TEXT    NOT NULL,
    callsign          TEXT,
    registration      TEXT,
    aircraft_type     TEXT,
    category          TEXT,
    lat               REAL    NOT NULL,
    lon               REAL    NOT NULL,
    alt_baro_ft       REAL,
    alt_geom_ft       REAL,
    ground_speed_kt   REAL,
    track_deg         REAL,
    vertical_rate_fpm REAL,
    on_ground         INTEGER NOT NULL DEFAULT 0,
    observed_at       TEXT    NOT NULL,
    source_api        TEXT    NOT NULL,
    UNIQUE(icao_hex, observed_at)
);

CREATE TABLE IF NOT EXISTS rejected_observations (
    id               INTEGER PRIMARY KEY,
    raw_json         TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    observed_at      TEXT,
    source_api       TEXT
);

CREATE TABLE IF NOT EXISTS aircraft (
    icao_hex      TEXT PRIMARY KEY,
    n_number      TEXT,
    owner_name    TEXT,
    owner_state   TEXT,
    model         TEXT,
    manufacturer  TEXT,
    year_mfr      TEXT,
    operator_flag TEXT
);

CREATE TABLE IF NOT EXISTS flights (
    flight_id               TEXT PRIMARY KEY,
    icao_hex                TEXT NOT NULL,
    n_number                TEXT,
    operator_flag           TEXT,
    started_at              TEXT NOT NULL,
    ended_at                TEXT,
    departure_heliport      TEXT,
    arrival_heliport        TEXT,
    min_alt_baro_ft         REAL,
    max_alt_baro_ft         REAL,
    crossed_hoboken         INTEGER NOT NULL DEFAULT 0,
    min_alt_over_hoboken_ft REAL,
    time_in_hoboken_sec     REAL,
    total_observations      INTEGER NOT NULL DEFAULT 0,
    confidence              TEXT    NOT NULL DEFAULT 'high',
    track_summary           TEXT,
    is_kearny_departure     INTEGER NOT NULL DEFAULT 0,
    outside_hhi_hours       INTEGER NOT NULL DEFAULT 0,
    is_tour_operator        INTEGER NOT NULL DEFAULT 0,
    reconstructed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id                INTEGER PRIMARY KEY,
    flight_id         TEXT NOT NULL,
    recipient_channel TEXT NOT NULL,
    recipient_address TEXT NOT NULL,
    status            TEXT NOT NULL,
    submitted_at      TEXT,
    workflow_run_id   TEXT,
    error_message     TEXT,
    UNIQUE(flight_id, recipient_channel)
);

CREATE INDEX IF NOT EXISTS idx_obs_hex_time
    ON observations(icao_hex, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_time
    ON observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_lat_lon
    ON observations(lat, lon);
CREATE INDEX IF NOT EXISTS idx_flights_started
    ON flights(started_at);
CREATE INDEX IF NOT EXISTS idx_flights_kearny_time
    ON flights(is_kearny_departure, started_at);
CREATE INDEX IF NOT EXISTS idx_flights_hoboken_time
    ON flights(crossed_hoboken, started_at);
CREATE INDEX IF NOT EXISTS idx_flights_violations
    ON flights(outside_hhi_hours, is_kearny_departure, started_at);
CREATE INDEX IF NOT EXISTS idx_flights_hex_time
    ON flights(icao_hex, started_at);
"""

# ── Signal ────────────────────────────────────────────────────────────────────

_shutdown = False

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

def git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        capture_output=True, text=True,
    )

# ── Validation ────────────────────────────────────────────────────────────────

def validate(ac: dict, source: str, observed_at: str) -> tuple:
    """
    Normalize and validate one aircraft record from the API.
    Returns (row_dict, None) on success or (None, reason_str) on failure.
    """
    raw_hex = str(ac.get("hex", "")).strip().lstrip("~").upper()
    if not ICAO_RE.match(raw_hex):
        return None, f"bad icao_hex={raw_hex!r}"

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

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn

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

# ── Git commit/push ───────────────────────────────────────────────────────────

def commit_push(data_dir: Path, n_obs: int, ts: str) -> None:
    git(data_dir, "add", "flights.db")

    if git(data_dir, "diff", "--cached", "--quiet").returncode == 0:
        log.info("commit: nothing new to push at %s", ts)
        return

    r = git(data_dir, "commit", "-m", f"harvest: {ts} ({n_obs} new obs)")
    if r.returncode != 0:
        log.warning("git commit failed: %s", r.stderr.strip())
        return

    r = git(data_dir, "push", "--force-with-lease", "origin", "data")
    if r.returncode == 0:
        log.info("pushed: harvest %s (%d new obs)", ts, n_obs)
        return

    # First push failed — fetch latest remote ref and retry once.
    # This handles the rare case of a concurrent push from another runner.
    log.warning("push failed (%s) — fetching and retrying once", r.stderr.strip())
    git(data_dir, "fetch", "origin", "data")
    r = git(data_dir, "push", "--force-with-lease", "origin", "data")
    if r.returncode == 0:
        log.info("push succeeded on retry")
    else:
        log.warning("push still failed: %s — will retry at next commit window",
                    r.stderr.strip())

# ── Main loop ─────────────────────────────────────────────────────────────────

def run(duration_sec: int, db_path: Path, data_dir: Path) -> None:
    global _shutdown
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
        poll_start = time.monotonic()

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

            # Drop the batch if the API's reported timestamp is severely skewed
            api_now = payload.get("now")
            if api_now is not None:
                skew = abs(time.time() - float(api_now))
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
            time.sleep(max(0.0, POLL_INTERVAL_SEC - (time.monotonic() - poll_start)))
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
            commit_push(data_dir, obs_window, iso(utcnow()))
            obs_window  = 0
            next_commit = time.monotonic() + COMMIT_INTERVAL_SEC

        # ── Sleep to maintain 10-sec cadence ─────────────────────────────────
        time.sleep(max(0.0, POLL_INTERVAL_SEC - (time.monotonic() - poll_start)))

    # ── Shutdown: final commit ────────────────────────────────────────────────
    commit_push(data_dir, obs_window, iso(utcnow()))
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
    main()
