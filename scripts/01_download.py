"""
01_download.py — acquire all raw data for the Branch Network Strategy project.

Downloads to data/raw/ and writes manifest.json recording, for every file:
source URL, retrieval timestamp, byte size, and SHA-256.

The manifest is a portfolio artifact, not bookkeeping. Federal datasets have
been removed and altered at scale during 2025-2026; being able to say exactly
which vintage was pulled, from where, and when, is part of the deliverable.

Usage:
    $env:CENSUS_API_KEY = "..."      # https://api.census.gov/data/key_signup.html
    python scripts/01_download.py
    python scripts/01_download.py --only acs   # single source

ENDPOINT STATUS — all verified live 2026-08-14. See docs/data_quality_log.md
for what changed and why. Two things worth knowing before you read further:

  - The FDIC Summary of Deposits BULK ZIP IS GONE. The historic
    ShowFileWithStats1.asp pattern 404s on every host. SOD is now pulled from
    the FDIC's REST API, filtered to WI+IL, which is both smaller and more
    honest about what we actually use. Consequence for script 02: the API
    normalizes field names across vintages, so the schema-drift profiling that
    02 was written to do will find far less than expected. That is a real loss
    of a portfolio narrative - see the quality log.

  - The HMDA public panel ZIP IS GONE (S3 returns AccessDenied) and the
    Federal Reserve NPW bulk download is behind a CAPTCHA. Both were the
    intended sources for the RSSD -> LEI bridge. The FFIEC institutions API
    exposes an `rssd` field but returns -1 for every institution tested,
    including the subject candidates. THE CROSSWALK IN SCRIPT 04 HAS NO
    AUTOMATED SOURCE RIGHT NOW. What we can still get is the full list of
    2025 HMDA filers (LEI + name), which is downloaded below. Read the
    quality log before starting script 04 - this is gate 5.4 and it is at risk.
"""

from __future__ import annotations

import argparse
import csv
import gzip
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
DELINEATION_YEAR = 2023         # current OMB CBSA delineation; 2025 does not exist
STATE_FIPS = {"WI": "55", "IL": "17"}

TIMEOUT = 300
CHUNK = 1 << 20
PAGE = 10000                    # FDIC API maximum rows per request

# Manifest entries from a previous run, keyed by filename. Used so that an
# already-present file keeps its ORIGINAL retrieval timestamp instead of being
# downgraded to "pre-existing" on every re-run. Populated in main().
PRIOR: dict[str, dict] = {}

# --------------------------------------------------------------------------
# Source definitions — every URL below returned real data on 2026-08-14
# --------------------------------------------------------------------------

# FDIC Summary of Deposits, REST API. Replaces the dead bulk ZIP.
# Docs: https://api.fdic.gov/banks/docs/
# DEPSUMBR is branch deposits IN THOUSANDS. UNINUMBR is the stable unique
# branch identifier and is the real primary key for dim_branch - the "branch
# number" named in the design docs is only unique within an institution.
SOD_API = "https://api.fdic.gov/banks/sod"

# FDIC institutions, REST API. FED_RSSD confirmed present and populated.
# Pulled for ALL states, not just WI+IL: institutions headquartered elsewhere
# (Chase, US Bank, BMO) operate branches in our footprint and appear in SOD,
# and without them dim_institution has null assets and RSSD for a large share
# of competitor certs.
FDIC_INSTITUTIONS_API = "https://api.fdic.gov/banks/institutions"
FDIC_INSTITUTION_FIELDS = "CERT,NAME,STALP,CITY,ASSET,FED_RSSD,ACTIVE,CLASS"

# HMDA loan-level data, FFIEC Data Browser filtered-query endpoint.
# Docs: https://ffiec.cfpb.gov/documentation/api/data-browser/
# NOTE FOR THE CASE STUDY: this endpoint serves the SNAPSHOT dataset filtered
# to the requested states - it is not the "Modified LAR" that every design doc
# calls it. The distinction matters and 05 s.2 flags confusing them as the
# classic HMDA error. State plainly which file was used. Roughly 93 MB for WI
# alone, so expect ~200 MB for WI+IL.
HMDA_LAR_URL = (
    "https://ffiec.cfpb.gov/v2/data-browser-api/view/csv"
    "?states=WI,IL&years={year}"
)

# HMDA filer list — LEI and name for every institution that filed in the year.
# This is what remains of the panel. It carries NO RSSD, so it confirms that a
# candidate filed (UAT-10, criterion 4) but does not by itself bridge to FDIC.
HMDA_FILERS_URL = "https://ffiec.cfpb.gov/v2/reporting/filers/{year}"

# Census ACS 5-year API. Key required for all data queries.
ACS_TRACT_VARS = [
    "B01003_001E",  # total population
    "B19013_001E",  # median household income
    "B19113_001E",  # median FAMILY income - the LMI numerator (see below)
    "B25003_001E",  # occupied housing units (tenure universe = households)
    "B25003_002E",  # owner occupied
    "B25077_001E",  # median home value
]
ACS_TRACT_URL = (
    "https://api.census.gov/data/{year}/acs/acs5"
    "?get=NAME,{vars}&for=tract:*&in=state:{fips}"
)

# ACS at CBSA level. THIS IS THE LMI DENOMINATOR AND IT WAS MISSING ENTIRELY.
# The FFIEC definition of a low- or moderate-income tract is tract median
# family income below 80% of the AREA median family income. Tract-level data
# alone cannot produce that ratio - it has no denominator. Without this pull,
# dim_tract.lmi_flag cannot be computed, and AC-06 and BQ-6 both depend on it.
ACS_CBSA_URL = (
    "https://api.census.gov/data/{year}/acs/acs5"
    "?get=NAME,B19013_001E,B19113_001E"
    "&for=metropolitan%20statistical%20area/micropolitan%20statistical%20area:*"
)

# OMB / Census CBSA delineation file: county FIPS -> CBSA code, title, and
# metro-vs-micro status. ALSO MISSING ENTIRELY. config/catchment.yaml keys the
# urban (3mi) vs non-urban (8mi) radius rule on CBSA membership, dim_tract
# carries a cbsa column, SQL-04 ranks within CBSA, and FR-01 filters by it.
# Nothing downloaded before today supplied any of that.
DELINEATION_URL = (
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/"
    "reference-files/{year}/delineation-files/list1_{year}.xlsx"
)

# TIGER/Line tract shapefiles. Must match the ACS vintage (2020 boundaries).
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


def entry_for(dest: Path, url: str, label: str) -> dict:
    """Manifest entry for a file already on disk.

    Reuses the prior entry when the checksum still matches, so that a re-run
    preserves the original retrieval timestamp. The whole point of the
    manifest is knowing when a vintage was pulled; overwriting that with
    "pre-existing" on every re-run destroys the artifact.
    """
    digest = sha256(dest)
    prior = PRIOR.get(dest.name)
    if prior and prior.get("sha256") == digest:
        return prior
    return {
        "file": dest.name, "url": url, "label": label,
        "retrieved_utc": "unknown (present before first manifest)",
        "bytes": dest.stat().st_size, "sha256": digest,
    }


def fetch(url: str, dest: Path, label: str, params: dict | None = None,
          redact: str | None = None) -> dict | None:
    """Stream a URL to disk. Returns a manifest entry, or None on failure."""
    if dest.exists():
        print(f"  [skip] {dest.name} already present")
        return entry_for(dest, url, label)

    print(f"  [get ] {label} -> {dest.name}")
    started = time.time()
    try:
        with requests.get(url, params=params, stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            final_url = resp.url
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype.lower():
                # A landing page came back instead of a file. Almost always a
                # changed endpoint. Fail loudly rather than saving HTML as data.
                print(f"  [FAIL] {label}: got HTML, not data. Endpoint moved?")
                print(f"         {final_url}")
                return None
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    fh.write(chunk)
            tmp.replace(dest)
    except requests.RequestException as exc:
        print(f"  [FAIL] {label}: {exc}")
        return None

    size = dest.stat().st_size
    if size < 1024:
        print(f"  [WARN] {dest.name} is only {size} bytes - inspect it")

    print(f"         {size:,} bytes in {time.time() - started:.1f}s")
    if redact:
        final_url = final_url.replace(redact, "REDACTED")
    return {
        "file": dest.name, "url": final_url, "label": label,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "bytes": size, "sha256": sha256(dest),
    }


def fetch_fdic_paged(api: str, filters: str, dest: Path, label: str,
                     fields: str | None = None) -> dict | None:
    """Page through an FDIC API endpoint and write one CSV.

    The API caps a response at 10,000 rows, so anything larger needs offset
    paging. Rows are written with a single header; the union of keys across
    pages is used so a field appearing only in later vintages is not dropped.
    """
    if dest.exists():
        print(f"  [skip] {dest.name} already present")
        return entry_for(dest, api, label)

    print(f"  [get ] {label} -> {dest.name}")
    started = time.time()
    rows: list[dict] = []
    offset = 0
    try:
        while True:
            params = {"filters": filters, "limit": PAGE,
                      "offset": offset, "format": "json"}
            if fields:
                params["fields"] = fields
            resp = requests.get(api, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            page = [item["data"] for item in payload.get("data", [])]
            rows.extend(page)
            total = payload.get("meta", {}).get("total", len(rows))
            if len(page) < PAGE or len(rows) >= total:
                break
            offset += PAGE
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"  [FAIL] {label}: {exc}")
        return None

    if not rows:
        print(f"  [FAIL] {label}: zero rows returned")
        return None

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    # newline="" per csv docs; utf-8 explicitly so the file reads identically
    # on any platform (this repo is developed on Windows, read on Linux CI).
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    size = dest.stat().st_size
    print(f"         {len(rows):,} rows, {size:,} bytes in {time.time() - started:.1f}s")
    return {
        "file": dest.name, "url": f"{api}?filters={filters}", "label": label,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "bytes": size, "sha256": sha256(dest), "rows": len(rows),
    }


def get_sod(entries: list, failures: list) -> None:
    print("\nFDIC Summary of Deposits, 7 vintages, WI+IL (REST API)")
    for year in SOD_YEARS:
        for state in STATE_FIPS:
            entry = fetch_fdic_paged(
                SOD_API,
                f"STALP:{state} AND YEAR:{year}",
                RAW / f"fdic_sod_{year}_{state.lower()}.csv",
                f"SOD {year} {state}",
            )
            (entries if entry else failures).append(entry or f"SOD {year} {state}")


def get_fdic_institutions(entries: list, failures: list) -> None:
    print("\nFDIC institutions, all states (for the RSSD crosswalk)")
    entry = fetch_fdic_paged(
        FDIC_INSTITUTIONS_API,
        "CERT:[0 TO 1000000]",
        RAW / "fdic_institutions_all.csv",
        "FDIC institutions (all)",
        fields=FDIC_INSTITUTION_FIELDS,
    )
    (entries if entry else failures).append(entry or "FDIC institutions")


# Columns kept in the committed subset of the loan-level file. The full file
# is 266 MB - over GitHub's 100 MB per-file limit - so what gets committed for
# durability (05 s.1) is this reduced version, and the manifest pins the
# SHA-256 of the full file it came from.
#
# The rule for what stays: everything needed to rebuild fact_tract_lending
# (tract x lei x loan_purpose x action_taken) plus the tract-level context
# HMDA supplies. Applicant-level demographics are deliberately NOT carried.
# They are not inputs to the index, the equity check runs on income class
# rather than race (05 s.4), and there is no reason to commit applicant
# characteristics to a public repository to answer BQ-4 or BQ-6.
#
# tract_to_msa_income_percentage is the important one: it is the FFIEC's own
# tract-income-to-area-income ratio, and below 80 IS the regulatory definition
# of a low- or moderate-income tract. It is a better basis for lmi_flag than
# anything reconstructed from ACS, because it is the figure examiners use.
HMDA_SUBSET_COLUMNS = [
    "activity_year", "lei", "derived_msa-md", "state_code", "county_code",
    "census_tract", "derived_dwelling_category", "action_taken", "loan_type",
    "loan_purpose", "lien_status", "business_or_commercial_purpose",
    "loan_amount", "occupancy_type", "total_units", "denial_reason-1",
    "tract_population", "tract_minority_population_percent",
    "ffiec_msa_md_median_family_income", "tract_to_msa_income_percentage",
    "tract_owner_occupied_units",
]


def write_hmda_subset(source: Path, dest: Path) -> dict | None:
    """Reduce the loan-level file to committable size, streaming.

    Row count is preserved - this drops columns, never rows. Dropping rows
    would need to be asked about first, and would break the reproducibility
    of the tract aggregation in script 06.
    """
    if dest.exists():
        print(f"  [skip] {dest.name} already present")
        return entry_for(dest, f"derived from {source.name}", "HMDA tract subset")
    if not source.exists():
        print(f"  [FAIL] cannot build subset: {source.name} not downloaded")
        return None

    print(f"  [make] {dest.name} <- {source.name}")
    started = time.time()
    rows = 0
    # Written gzipped. Uncompressed the subset is ~95 MB, which clears
    # GitHub's 100 MiB hard limit but only just, and the next vintage would
    # not. gzip takes it to a size that will not become a problem later.
    # pandas and DuckDB both read .csv.gz directly, so nothing downstream
    # needs to decompress it first.
    with source.open(newline="", encoding="utf-8") as src, \
            gzip.open(dest, "wt", newline="", encoding="utf-8") as out:
        reader = csv.reader(src)
        header = next(reader)
        missing = [c for c in HMDA_SUBSET_COLUMNS if c not in header]
        if missing:
            print(f"  [FAIL] columns absent from the source file: {missing}")
            return None
        keep = [header.index(c) for c in HMDA_SUBSET_COLUMNS]
        writer = csv.writer(out)
        writer.writerow(HMDA_SUBSET_COLUMNS)
        for row in reader:
            writer.writerow([row[i] for i in keep])
            rows += 1

    size = dest.stat().st_size
    print(f"         {rows:,} rows, {len(HMDA_SUBSET_COLUMNS)} of {len(header)} "
          f"columns, {size:,} bytes in {time.time() - started:.1f}s")
    return {
        "file": dest.name,
        "url": f"derived locally from {source.name}",
        "label": "HMDA tract subset (committed in place of the full file)",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "bytes": size, "sha256": sha256(dest), "rows": rows,
        "derived_from": source.name,
        "columns_kept": HMDA_SUBSET_COLUMNS,
    }


def get_hmda(entries: list, failures: list) -> None:
    print("\nHMDA (loan-level + filer list)")
    lar = RAW / f"hmda_lar_{HMDA_YEAR}_wi_il.csv"
    entry = fetch(
        HMDA_LAR_URL.format(year=HMDA_YEAR),
        lar,
        f"HMDA loan-level {HMDA_YEAR} WI+IL (~266 MB, be patient)",
    )
    (entries if entry else failures).append(entry or "HMDA loan-level")

    entry = write_hmda_subset(lar, RAW / f"hmda_tract_subset_{HMDA_YEAR}_wi_il.csv.gz")
    (entries if entry else failures).append(entry or "HMDA subset")

    entry = fetch(
        HMDA_FILERS_URL.format(year=HMDA_YEAR),
        RAW / f"hmda_filers_{HMDA_YEAR}.json",
        f"HMDA filer list {HMDA_YEAR}",
    )
    (entries if entry else failures).append(entry or "HMDA filers")


def get_acs(entries: list, failures: list) -> None:
    print("\nACS 5-year (2020-2024)")
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        print("  [FAIL] CENSUS_API_KEY not set.")
        print("         Get one free: https://api.census.gov/data/key_signup.html")
        print("         PowerShell:  $env:CENSUS_API_KEY = 'your_key_here'")
        failures.append("ACS (no API key)")
        return

    for state, fips in STATE_FIPS.items():
        url = ACS_TRACT_URL.format(
            year=ACS_YEAR, vars=",".join(ACS_TRACT_VARS), fips=fips
        ) + f"&key={key}"
        entry = fetch(url, RAW / f"acs5_{ACS_YEAR}_tract_{state.lower()}.json",
                      f"ACS tracts {state}", redact=key)
        (entries if entry else failures).append(entry or f"ACS tracts {state}")

    # The LMI denominator - see ACS_CBSA_URL above.
    url = ACS_CBSA_URL.format(year=ACS_YEAR) + f"&key={key}"
    entry = fetch(url, RAW / f"acs5_{ACS_YEAR}_cbsa.json",
                  "ACS CBSA median income (LMI denominator)", redact=key)
    (entries if entry else failures).append(entry or "ACS CBSA")


def get_geography(entries: list, failures: list) -> None:
    print("\nGeography reference files")
    entry = fetch(
        DELINEATION_URL.format(year=DELINEATION_YEAR),
        RAW / f"cbsa_delineation_{DELINEATION_YEAR}.xlsx",
        f"CBSA delineation {DELINEATION_YEAR}",
    )
    (entries if entry else failures).append(entry or "CBSA delineation")

    for state, fips in STATE_FIPS.items():
        entry = fetch(
            TIGER_URL.format(year=TIGER_YEAR, fips=fips),
            RAW / f"tl_{TIGER_YEAR}_{fips}_tract.zip",
            f"TIGER tracts {state}",
        )
        (entries if entry else failures).append(entry or f"TIGER {state}")


SOURCES = {
    "sod": get_sod,
    "fdic": get_fdic_institutions,
    "hmda": get_hmda,
    "acs": get_acs,
    "geo": get_geography,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=sorted(SOURCES), help="one source only")
    args = parser.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)

    if MANIFEST.exists():
        PRIOR.update({e["file"]: e
                      for e in json.loads(MANIFEST.read_text()).get("files", [])})

    entries: list = []
    failures: list = []

    for name, fn in SOURCES.items():
        if args.only and name != args.only:
            continue
        fn(entries, failures)

    good = [e for e in entries if isinstance(e, dict)]
    if good:
        by_file = dict(PRIOR)
        by_file.update({e["file"]: e for e in good})
        files = sorted(by_file.values(), key=lambda e: e["file"])
        was = sorted(PRIOR.values(), key=lambda e: e["file"])
        # Only rewrite when the file set actually changed. Stamping a fresh
        # written_utc on a no-op re-run makes `git status` dirty every time
        # the script is run, which trains the reader to ignore that signal.
        if files == was and MANIFEST.exists():
            print(f"\nManifest unchanged: {MANIFEST}")
        else:
            MANIFEST.write_text(json.dumps(
                {
                    "project": "branch-network-strategy",
                    "written_utc": datetime.now(timezone.utc).isoformat(),
                    "files": files,
                },
                indent=2,
            ))
            print(f"\nManifest: {MANIFEST}")

    print(f"\n{'=' * 60}")
    print(f"Retrieved: {len(good)} file(s)")
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        print("\nIf an endpoint moved, correct it above and log the change in")
        print("docs/data_quality_log.md before re-running.")
        return 1

    print("All sources retrieved. Next: python scripts/02_profile_sod.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
