"""
01_download.py — acquire all raw data for the Branch Network Strategy project.

Downloads to data/raw/ and writes manifest.json recording, for every file:
source URL, retrieval timestamp, byte size, and SHA-256.

The manifest is a portfolio artifact, not bookkeeping. Federal datasets have
been removed and altered at scale during 2025-2026; being able to say exactly
which vintage was pulled, from where, and when, is part of the deliverable.

Usage:
    export CENSUS_API_KEY=...        # https://api.census.gov/data/key_signup.html
    python scripts/01_download.py
    python scripts/01_download.py --only acs   # single source

NOTE ON URLS MARKED "VERIFY":
Endpoints change. Anything tagged VERIFY below was not confirmed against a live
response when this script was written. Open the listed landing page, confirm the
pattern, correct it here, and record the correction in docs/data_quality_log.md.
Do not guess a URL into working - a wrong-but-successful download is worse than
a failure, because it is silent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
MANIFEST = RAW / "manifest.json"

SOD_YEARS = range(2019, 2026)   # 7 vintages - see 09-project-a-decisions.md
HMDA_YEAR = 2025
ACS_YEAR = 2024                 # 2020-2024 5-year estimates
TIGER_YEAR = 2024
STATE_FIPS = {"WI": "55", "IL": "17"}

TIMEOUT = 300
CHUNK = 1 << 20

# --------------------------------------------------------------------------
# Source definitions
# --------------------------------------------------------------------------

# VERIFY - landing page: https://www.fdic.gov/foia/summary-deposits-data-bulk-download
# Historic pattern was ShowFileWithStats1.asp?strFileName=ALL_<year>.zip on the
# www5/www7 hosts. Confirm the current host and casing before running.
SOD_URL = "https://www7.fdic.gov/sod/ShowFileWithStats1.asp?strFileName=ALL_{year}.zip"

# CONFIRMED pattern - HMDA Data Browser CSV endpoint.
# Docs: https://ffiec.cfpb.gov/documentation/api/data-browser/
# Streams; the WI+IL 2025 file is large. Expect a long download.
HMDA_LAR_URL = (
    "https://ffiec.cfpb.gov/v2/data-browser-api/view/csv"
    "?states=WI,IL&years={year}"
)

# VERIFY - landing page:
# https://ffiec.cfpb.gov/data-publication/snapshot-national-loan-level-dataset/{year}
# The panel file carries the NIC/RSSD identifiers needed for the crosswalk.
HMDA_PANEL_URL = (
    "https://s3.amazonaws.com/cfpb-hmda-public/prod/snapshot-data/"
    "{year}/{year}_public_panel_csv.zip"
)

# VERIFY field names against https://api.fdic.gov/banks/docs
# FED_RSSD is the bridge to HMDA. If it is absent or renamed, the crosswalk
# in script 04 has no anchor - stop and reassess rather than working around it.
FDIC_INSTITUTIONS_URL = (
    "https://banks.data.fdic.gov/api/institutions"
    "?filters=STALP:{state}"
    "&fields=CERT,NAME,STALP,ASSET,FED_RSSD,ACTIVE"
    "&limit=10000&format=csv&download=true"
)

# CONFIRMED pattern - Census ACS 5-year API. Key required for all queries.
ACS_VARS = [
    "B01003_001E",  # total population
    "B19013_001E",  # median household income
    "B25003_001E",  # occupied housing units (tenure universe = households)
    "B25003_002E",  # owner occupied
    "B25077_001E",  # median home value
]
ACS_URL = (
    "https://api.census.gov/data/{year}/acs/acs5"
    "?get=NAME,{vars}&for=tract:*&in=state:{fips}&key={key}"
)

# CONFIRMED pattern - TIGER/Line tract shapefiles.
TIGER_URL = (
    "https://www2.census.gov/geo/tiger/TIGER{year}/TRACT/"
    "tl_{year}_{fips}_tract.zip"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dest: Path, label: str) -> dict | None:
    """Stream a URL to disk. Returns a manifest entry, or None on failure."""
    if dest.exists():
        print(f"  [skip] {dest.name} already present")
        return {
            "file": dest.name, "url": url, "label": label,
            "retrieved_utc": "pre-existing",
            "bytes": dest.stat().st_size, "sha256": sha256(dest),
        }

    print(f"  [get ] {label} -> {dest.name}")
    started = time.time()
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype.lower():
                # A landing page came back instead of a file. Almost always a
                # changed endpoint. Fail loudly rather than saving HTML as data.
                print(f"  [FAIL] {label}: got HTML, not data. Endpoint moved?")
                print(f"         {url}")
                return None
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    fh.write(chunk)
            tmp.rename(dest)
    except requests.RequestException as exc:
        print(f"  [FAIL] {label}: {exc}")
        return None

    size = dest.stat().st_size
    if size < 1024:
        print(f"  [WARN] {dest.name} is only {size} bytes - inspect it")

    print(f"         {size:,} bytes in {time.time() - started:.1f}s")
    return {
        "file": dest.name, "url": url, "label": label,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "bytes": size, "sha256": sha256(dest),
    }


def get_sod(entries: list, failures: list) -> None:
    print("\nFDIC Summary of Deposits (7 vintages)")
    for year in SOD_YEARS:
        url = SOD_URL.format(year=year)
        entry = fetch(url, RAW / f"fdic_sod_{year}.zip", f"SOD {year}")
        (entries if entry else failures).append(entry or f"SOD {year}")


def get_hmda(entries: list, failures: list) -> None:
    print("\nHMDA (LAR + panel)")
    entry = fetch(
        HMDA_LAR_URL.format(year=HMDA_YEAR),
        RAW / f"hmda_lar_{HMDA_YEAR}_wi_il.csv",
        f"HMDA LAR {HMDA_YEAR} WI+IL",
    )
    (entries if entry else failures).append(entry or "HMDA LAR")

    entry = fetch(
        HMDA_PANEL_URL.format(year=HMDA_YEAR),
        RAW / f"hmda_panel_{HMDA_YEAR}.zip",
        f"HMDA panel {HMDA_YEAR}",
    )
    (entries if entry else failures).append(entry or "HMDA panel")


def get_fdic_institutions(entries: list, failures: list) -> None:
    print("\nFDIC institutions (for the RSSD crosswalk)")
    for state in STATE_FIPS:
        entry = fetch(
            FDIC_INSTITUTIONS_URL.format(state=state),
            RAW / f"fdic_institutions_{state.lower()}.csv",
            f"FDIC institutions {state}",
        )
        (entries if entry else failures).append(entry or f"FDIC inst {state}")


def get_acs(entries: list, failures: list) -> None:
    print("\nACS 5-year (2020-2024)")
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        print("  [FAIL] CENSUS_API_KEY not set.")
        print("         Get one free: https://api.census.gov/data/key_signup.html")
        failures.append("ACS (no API key)")
        return

    for state, fips in STATE_FIPS.items():
        url = ACS_URL.format(
            year=ACS_YEAR, vars=",".join(ACS_VARS), fips=fips, key=key
        )
        dest = RAW / f"acs5_{ACS_YEAR}_tract_{state.lower()}.json"
        entry = fetch(url, dest, f"ACS {state}")
        if entry:
            # Redact the key before it reaches a committed manifest.
            entry["url"] = entry["url"].replace(key, "REDACTED")
            entries.append(entry)
        else:
            failures.append(f"ACS {state}")


def get_tiger(entries: list, failures: list) -> None:
    print("\nTIGER/Line tract shapefiles")
    for state, fips in STATE_FIPS.items():
        entry = fetch(
            TIGER_URL.format(year=TIGER_YEAR, fips=fips),
            RAW / f"tl_{TIGER_YEAR}_{fips}_tract.zip",
            f"TIGER tracts {state}",
        )
        (entries if entry else failures).append(entry or f"TIGER {state}")


SOURCES = {
    "sod": get_sod,
    "hmda": get_hmda,
    "fdic": get_fdic_institutions,
    "acs": get_acs,
    "tiger": get_tiger,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=sorted(SOURCES), help="one source only")
    args = parser.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    entries: list = []
    failures: list = []

    for name, fn in SOURCES.items():
        if args.only and name != args.only:
            continue
        fn(entries, failures)

    if entries:
        existing = []
        if MANIFEST.exists():
            existing = json.loads(MANIFEST.read_text()).get("files", [])
        by_file = {e["file"]: e for e in existing}
        by_file.update({e["file"]: e for e in entries if isinstance(e, dict)})
        MANIFEST.write_text(json.dumps(
            {
                "project": "branch-network-strategy",
                "written_utc": datetime.now(timezone.utc).isoformat(),
                "files": sorted(by_file.values(), key=lambda e: e["file"]),
            },
            indent=2,
        ))
        print(f"\nManifest: {MANIFEST}")

    print(f"\n{'=' * 60}")
    print(f"Downloaded: {len([e for e in entries if isinstance(e, dict)])}")
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        print("\nFix the endpoints marked VERIFY at the top of this file,")
        print("then log what changed in docs/data_quality_log.md.")
        return 1

    print("All sources retrieved. Next: python scripts/02_profile_sod.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
