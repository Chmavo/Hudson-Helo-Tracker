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
REGISTRY_MAX_AGE_DAYS = 30
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

def load_operators(path: Path) -> tuple[list, dict]:
    """Parse operators.yml.

    Returns (owner_name_terms, callsign_prefix_map).
      owner_name_terms   — substrings matched against FAA owner_name
      callsign_prefix_map — {UPPER_PREFIX: display_name} for ADS-B callsign matching
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


def match_operator(owner_name: str, owner_names: list) -> str | None:
    lower = owner_name.lower()
    for op in owner_names:
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
    return f"faa-registry-{now.year}-{now.month:02d}"


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
        # registry.faa.gov blocks obvious automation User-Agents from cloud IPs;
        # use browser-like headers so the download succeeds on GitHub Actions.
        faa_headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/zip,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://registry.faa.gov/aircraftinquiry/Search/NNumberInquiry",
        }
        r = session.get(FAA_REGISTRY_URL, timeout=180, headers=faa_headers)
        if r.status_code == 403:
            log.warning(
                "FAA registry returned 403 (IP block or rate limit). "
                "Enrichment skipped — will retry on next scheduled run."
            )
            return None
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
    """Normalize DictReader field names: collapse whitespace and remove leading BOM.

    FAA CSVs opened with latin-1 encoding may have a UTF-8 BOM prepended to the
    first field as the three latin-1 characters \\xef\\xbb\\xbf (ï»¿).  Internal
    column names like 'MODE S CODE HEX' also need whitespace collapsed, not just
    stripped at the edges.
    """
    _ = reader.fieldnames          # trigger header row read
    names = [' '.join(h.split()) for h in (reader.fieldnames or [])]
    if names:
        # UTF-8 BOM decoded as latin-1 is the three characters \xef \xbb \xbf
        names[0] = names[0].lstrip('\xef\xbb\xbf')
    reader.fieldnames = names


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
            log.info("ACFTREF.txt fieldnames: %s", reader.fieldnames[:8] if reader.fieldnames else [])
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
            log.info("MASTER.txt fieldnames: %s", reader.fieldnames[:12] if reader.fieldnames else [])

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

    owner_names, callsign_prefixes = load_operators(Path("operators.yml"))
    log.info("Loaded %d owner-name terms, %d callsign prefixes from operators.yml",
             len(owner_names), len(callsign_prefixes))

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    zip_data = get_registry_zip(repo, session)
    if zip_data is None:
        log.warning("No registry data available — exiting without updating aircraft table.")
        sys.exit(0)
    aircraft = parse_registry(zip_data)

    conn = init_db(args.db_path)
    total = update_aircraft_table(conn, aircraft, owner_names)
    conn.close()
    log.info("aircraft table: %d rows total", total)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_push(args.data_dir, f"enrich: {ts} ({len(aircraft)} aircraft)", args.db_path)


if __name__ == "__main__":
    main()
