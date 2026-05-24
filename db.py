"""
Shared database schema, initialization, and git helpers.

All scripts (harvester, enrich, reconstruct, dashboard) import from here
so the schema stays in one place and git push logic isn't duplicated.
"""

import logging
import sqlite3
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# ── Full schema (all stages) ──────────────────────────────────────────────────
#
# Every CREATE TABLE/INDEX uses IF NOT EXISTS so any script can call
# init_db() safely regardless of which scripts have run before it.

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


def init_db(path: Path) -> sqlite3.Connection:
    """Open (or create) flights.db, apply the full schema, return connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        capture_output=True, text=True,
    )


def commit_push(data_dir: Path, message: str,
                db_path: Path | None = None) -> None:
    """Stage flights.db, commit with message, push to origin/data.

    db_path: if supplied, a WAL checkpoint is run first so all in-memory WAL
    pages are flushed back to the main database file before git stages it.
    Without this, SQLite WAL mode keeps recent writes in flights.db-wal and
    git add flights.db sees no diff.
    """
    if db_path is not None:
        try:
            tmp = sqlite3.connect(str(db_path))
            tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            tmp.close()
        except Exception as exc:
            log.warning("WAL checkpoint failed: %s", exc)

    git(data_dir, "add", "flights.db")

    if git(data_dir, "diff", "--cached", "--quiet").returncode == 0:
        log.info("commit: nothing new to push")
        return

    r = git(data_dir, "commit", "-m", message)
    if r.returncode != 0:
        log.warning("git commit failed: %s", r.stderr.strip())
        return

    r = git(data_dir, "push", "--force-with-lease", "origin", "data")
    if r.returncode == 0:
        log.info("pushed: %s", message)
        return

    log.warning("push failed (%s) — fetching and retrying once", r.stderr.strip())
    git(data_dir, "fetch", "origin", "data")
    r = git(data_dir, "push", "--force-with-lease", "origin", "data")
    if r.returncode == 0:
        log.info("push succeeded on retry")
    else:
        log.warning("push still failed: %s — will retry next opportunity",
                    r.stderr.strip())
