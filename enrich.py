#!/usr/bin/env python3
"""
FAA aircraft registry enrichment for Hoboken Helo Accountability Tracker.

Downloads the FAA ReleasableAircraft.zip (from a GitHub Release cache or
fresh from registry.faa.gov if older than 90 days), parses MASTER.txt and
ACFTREF.txt, and upserts the aircraft table in flights.db.

Usage:
    python enrich.py [--db-path PATH] [--data-dir PATH]

Required env vars (auto-set by GitHub Actions):
    GITHUB_TOKEN        — for listing/creating/reading GitHub Releases
    GITHUB_REPOSITORY   — "owner/repo" (e.g. "chmavo/hudson-helo-tracker")
"""

import argparse
import csv
import io
import json
import logging
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from db import init_db, commit_push

VERSION  = "0.1"
REPO_URL = "https://github.com/chmavo/hudson-helo-tracker"
USER_AGENT = f"hoboken-helo-accountability/{VERSION} (+{REPO_URL})"

FAA_REGISTRY_URL    = "https://registry.faa.gov/database/ReleasableAircraft.zip"
REGISTRY_MAX_AGE_DAYS = 90
GITHUB_API          = "https://api.github.com"

# Suffix alphabet for N-number arithmetic (A-Z minus I and O = 24 chars)
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── N-number arithmetic ───────────────────────────────────────────────────────

def icao_to_n_number(icao_hex: str) -> str | None:
    """
    Derive an N-number from a US ICAO 24-bit address using the FAA's sequential
    allocation scheme.

    The FAA encodes N-numbers into 0xA00001–0xAFFFFF with 601 consecutive ICAO
    codes per numeric slot (1 bare + 24 single-letter suffixes + 576 two-letter
    suffixes using the 24-char alphabet A-Z minus I and O). This covers all
    N-numbers from N1 through approximately N1744 with their full suffix range.

    For the definitive lookup, use the MODE S CODE HEX column in MASTER.txt.
    This function is used as a display fallback for aircraft seen in the ADS-B
    stream that are not yet in the local registry snapshot.

    Reference: FAA N-number assignment per ICAO Annex 10 / DOT-VNTSC-FAA-99-7.
    """
    try:
        icao_int = int(icao_hex, 16)
    except ValueError:
        return None
    if not (0xA00001 <= icao_int <= 0xAFFFFF):
        return None

    n    = icao_int - 0xA00001
    slot = n // 601 + 1   # 1-based numeric part of N-number (N1…N99999)
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


def is_us_icao(icao_hex: str) -> bool:
    """Return True if hex falls in the US ICAO allocation range."""
    try:
        return 0xA00001 <= int(icao_hex, 16) <= 0xAFFFFF
    except ValueError:
        return False

# ── operators.yml ─────────────────────────────────────────────────────────────

def load_operators(path: Path) -> list:
    """Parse operators.yml without PyYAML. Returns list of match strings."""
    ops = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("- "):
                ops.append(s[2:].strip())
    return ops


def match_operator(owner_name: str, operators: list) -> str | None:
    lower = owner_name.lower()
    for op in operators:
        if op.lower() in lower:
            return op
    return None

# ── GitHub Release helpers ────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _list_releases(repo: str, session: requests.Session) -> list:
    r = session.get(f"{GITHUB_API}/repos/{repo}/releases",
                    headers=_gh_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _latest_faa_release(repo: str, session: requests.Session) -> dict | None:
    releases = _list_releases(repo, session)
    faa = [r for r in releases if r["tag_name"].startswith("faa-registry-")]
    if not faa:
        return None
    return sorted(faa, key=lambda r: r["created_at"], reverse=True)[0]


def _release_age_days(release: dict) -> int:
    created = (datetime
               .fromisoformat(release["created_at"].rstrip("Z"))
               .replace(tzinfo=timezone.utc))
    return (datetime.now(timezone.utc) - created).days


def _faa_release_tag() -> str:
    now = datetime.now(timezone.utc)
    q   = (now.month - 1) // 3 + 1
    return f"faa-registry-{now.year}Q{q}"


def _create_faa_release(repo: str, zip_data: bytes,
                        session: requests.Session) -> dict:
    tag = _faa_release_tag()
    log.info("Creating GitHub Release %s (%d bytes)", tag, len(zip_data))

    r = session.post(
        f"{GITHUB_API}/repos/{repo}/releases",
        headers=_gh_headers(),
        json={
            "tag_name":   tag,
            "name":       f"FAA Registry {tag}",
            "body":       ("FAA ReleasableAircraft.zip — "
                           "auto-downloaded from registry.faa.gov"),
            "draft":      False,
            "prerelease": False,
        },
        timeout=30,
    )
    r.raise_for_status()
    release = r.json()

    upload_url = (
        f"https://uploads.github.com/repos/{repo}/releases"
        f"/{release['id']}/assets?name=ReleasableAircraft.zip"
    )
    r = session.post(
        upload_url,
        headers={**_gh_headers(), "Content-Type": "application/zip"},
        data=zip_data,
        timeout=120,
    )
    r.raise_for_status()
    log.info("Uploaded ReleasableAircraft.zip to release %s", tag)
    return release

# ── Registry fetch ────────────────────────────────────────────────────────────

def get_registry_zip(repo: str, session: requests.Session) -> bytes:
    """
    Return the FAA ReleasableAircraft.zip bytes.

    Uses the most recent faa-registry-* GitHub Release if it's under 90 days
    old; otherwise downloads fresh from registry.faa.gov and publishes a new
    release so future runs use the cached copy.
    """
    release = _latest_faa_release(repo, session)
    needs_refresh = (
        release is None
        or _release_age_days(release) >= REGISTRY_MAX_AGE_DAYS
    )

    if needs_refresh:
        reason = ("no prior release"
                  if release is None
                  else f"release is {_release_age_days(release)}d old (≥{REGISTRY_MAX_AGE_DAYS}d)")
        log.info("Downloading fresh FAA registry (%s)...", reason)
        r = session.get(FAA_REGISTRY_URL, timeout=180,
                        headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        log.info("Downloaded %d bytes from registry.faa.gov", len(r.content))
        _create_faa_release(repo, r.content, session)
        return r.content

    # Use existing release asset
    assets = release.get("assets", [])
    asset  = next(
        (a for a in assets if a["name"] == "ReleasableAircraft.zip"), None
    )
    if not asset:
        raise RuntimeError(
            f"Release {release['tag_name']} has no ReleasableAircraft.zip asset"
        )

    log.info("Downloading ReleasableAircraft.zip from release %s (%dd old)...",
             release["tag_name"], _release_age_days(release))
    r = session.get(asset["browser_download_url"], timeout=180,
                    headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    log.info("Downloaded %d bytes from release asset", len(r.content))
    return r.content

# ── FAA registry parsing ──────────────────────────────────────────────────────

def _strip_fieldnames(reader: csv.DictReader) -> None:
    """Strip whitespace from DictReader field names (FAA files have trailing spaces)."""
    _ = reader.fieldnames          # trigger header row read
    reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]


def parse_registry(zip_data: bytes) -> dict:
    """
    Parse ReleasableAircraft.zip.

    Returns a dict keyed by uppercase ICAO hex, with values:
        {n_number, owner_name, owner_state, model, manufacturer, year_mfr}

    Primary source: MODE S CODE HEX column in MASTER.txt (definitive).
    Model/manufacturer joined from ACFTREF.txt via MFR MDL CODE.

    FAA MASTER.txt stores N-numbers without the leading 'N'; we add it.
    """
    log.info("Parsing FAA registry zip (%d bytes)...", len(zip_data))
    models   = {}
    aircraft = {}

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        # ── ACFTREF.txt: manufacturer/model by MFR MDL CODE ─────────────────
        with zf.open("ACFTREF.txt") as raw:
            wrapper = io.TextIOWrapper(raw, encoding="latin-1")
            reader  = csv.DictReader(wrapper)
            _strip_fieldnames(reader)
            for row in reader:
                code = row.get("CODE", "").strip()
                if code:
                    models[code] = {
                        "manufacturer": row.get("MFG",   "").strip(),
                        "model":        row.get("MODEL", "").strip(),
                    }
        log.info("ACFTREF.txt: %d model codes", len(models))

        # ── MASTER.txt: main aircraft registry ───────────────────────────────
        with zf.open("MASTER.txt") as raw:
            wrapper = io.TextIOWrapper(raw, encoding="latin-1")
            reader  = csv.DictReader(wrapper)
            _strip_fieldnames(reader)

            count = skipped = 0
            for row in reader:
                hex_code = row.get("MODE S CODE HEX", "").strip().upper()
                raw_n    = row.get("N-NUMBER", "").strip()
                if not hex_code or not raw_n:
                    skipped += 1
                    continue

                mfr_code   = row.get("MFR MDL CODE", "").strip()
                model_info = models.get(mfr_code, {})

                aircraft[hex_code] = {
                    "n_number":    f"N{raw_n}",   # FAA omits the 'N' prefix
                    "owner_name":  row.get("NAME",     "").strip(),
                    "owner_state": row.get("STATE",    "").strip(),
                    "model":       model_info.get("model",        ""),
                    "manufacturer": model_info.get("manufacturer", ""),
                    "year_mfr":    row.get("YEAR MFR", "").strip(),
                }
                count += 1

    log.info("MASTER.txt: %d aircraft parsed, %d skipped (missing hex/n)",
             count, skipped)
    return aircraft

# ── Database update ───────────────────────────────────────────────────────────

_UPSERT = """\
INSERT OR REPLACE INTO aircraft
    (icao_hex, n_number, owner_name, owner_state, model, manufacturer,
     year_mfr, operator_flag)
VALUES
    (:icao_hex, :n_number, :owner_name, :owner_state, :model, :manufacturer,
     :year_mfr, :operator_flag)
"""


def update_aircraft_table(conn: sqlite3.Connection,
                          aircraft: dict,
                          operators: list) -> int:
    """Upsert all parsed aircraft into the aircraft table. Returns total row count."""
    rows = [
        {
            "icao_hex":     hex_code,
            "n_number":     info["n_number"],
            "owner_name":   info["owner_name"],
            "owner_state":  info["owner_state"],
            "model":        info["model"],
            "manufacturer": info["manufacturer"],
            "year_mfr":     info["year_mfr"],
            "operator_flag": match_operator(info["owner_name"], operators),
        }
        for hex_code, info in aircraft.items()
    ]

    with conn:
        conn.executemany(_UPSERT, rows)

    return conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Hoboken Helo Accountability — FAA registry enrichment")
    p.add_argument("--db-path",  type=Path, default=Path("data-branch/flights.db"))
    p.add_argument("--data-dir", type=Path, default=Path("data-branch"))
    args = p.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log.error("GITHUB_REPOSITORY env var is required")
        sys.exit(1)

    operators = load_operators(Path("operators.yml"))
    log.info("Loaded %d operator match terms from operators.yml", len(operators))

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    zip_data = get_registry_zip(repo, session)
    aircraft = parse_registry(zip_data)

    conn = init_db(args.db_path)
    total = update_aircraft_table(conn, aircraft, operators)
    conn.close()
    log.info("aircraft table: %d rows total", total)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_push(args.data_dir, f"enrich: {ts} ({len(aircraft)} aircraft)", args.db_path)


if __name__ == "__main__":
    main()
