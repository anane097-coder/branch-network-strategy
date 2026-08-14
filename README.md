# Branch Network Strategy — Wisconsin & Illinois

An outside-in analysis of a regional bank's branch network, built entirely from
publicly filed federal data, to answer: **where should the next three branches
go, and which existing locations warrant review?**

> This analysis uses only publicly available regulatory filings. It reflects no
> relationship with, or confidential knowledge of, any institution discussed.

## Data

| Source | Vintage | What it provides |
|---|---|---|
| FDIC Summary of Deposits (REST API) | 2019–2025 | Deposits at every branch of every insured institution — 30,461 branch-years across WI+IL |
| HMDA loan-level (FFIEC Data Browser) | 2025 | 700,896 mortgage applications and outcomes by tract |
| HMDA filer list | 2025 | The 4,789 institutions that filed, by LEI |
| FDIC Institutions | current | Institution attributes including `FED_RSSD` |
| ACS 5-year, tract **and CBSA** | 2020–2024 | Tract demographics, income, housing, and the area-income denominator |
| CBSA delineation (OMB/Census) | 2023 | County → CBSA, metro vs. micro |
| TIGER/Line | 2024 | Census tract geometry — 4,807 tracts |

Every endpoint above was verified against a live response on 2026-08-14.
Several sources named in the design documents no longer exist in the form
those documents assume, and one required bridge dataset has disappeared
entirely. **[`docs/data_quality_log.md`](docs/data_quality_log.md) records
what changed and what it costs** — read it before running anything.

## Setup

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Free, instant: https://api.census.gov/data/key_signup.html
# Session only - set again each new terminal:
$env:CENSUS_API_KEY = "your_key_here"

python scripts\01_download.py
```

If `Activate.ps1` is blocked by execution policy, allow it for this session
only (does not change machine-wide settings):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

To persist the API key across terminals (run once, then reopen PowerShell):

```powershell
[Environment]::SetEnvironmentVariable("CENSUS_API_KEY", "your_key_here", "User")
```

**macOS / Linux**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CENSUS_API_KEY=your_key_here
python scripts/01_download.py
```

Requires **Python 3.14**. The dependency pins were raised on 2026-08-14 for it;
the originals had no wheels for 3.14 and could not install.

Downloads land in `data/raw/` alongside `manifest.json`, which records the
source URL, retrieval timestamp, byte size, and SHA-256 of every file.
Re-running is safe: files already present are skipped and keep their original
retrieval timestamp rather than being restamped.

**If a download fails**, the endpoint has likely moved. Confirm the current
pattern, correct it in `scripts/01_download.py`, and log the change in
`docs/data_quality_log.md`. The script refuses to save an HTML landing page as
data — a silent wrong download is worse than a loud failure.

### A note on the HMDA file

The full 2025 WI+IL loan-level extract is **266 MB**, over GitHub's 100 MiB
per-file limit, so it is not committed. What is committed is
`data/raw/hmda_tract_subset_2025_wi_il.csv.gz` — 12 MB, **all 700,896 rows**,
21 of the 99 columns: everything needed to rebuild the tract-grain lending
fact, plus HMDA's tract context. Columns were dropped; rows never were.
Applicant-level demographics were deliberately left out — they are not inputs
to the index, and the equity check runs on income class rather than race.

The manifest pins the SHA-256 of the full file, and `01_download.py`
reproduces it. This is the "documented subset" allowance in `08` §5.

## Running

Scripts are numbered and idempotent; run in order. A clean re-run from `raw/`
must reproduce identical outputs (acceptance criterion AC-04).

**Windows (PowerShell)**

```powershell
foreach ($s in Get-ChildItem scripts\[0-9]*.py | Sort-Object Name) {
    python $s.FullName
    if ($LASTEXITCODE -ne 0) { Write-Host "Stopped at $($s.Name)"; break }
}
```

(`foreach` rather than `ForEach-Object`: `break` inside a `ForEach-Object`
block has no enclosing loop to exit and terminates the whole pipeline
instead, which is a well-known PowerShell trap. UAT-09 hands this procedure
to another person to follow, so it needs to behave the way it reads.)

**macOS / Linux**

```bash
for s in scripts/[0-9]*.py; do python "$s" || break; done
```

## Repository layout

```
data/raw/         immutable downloads + manifest.json
data/staging/     typed, filtered to WI+IL
data/warehouse/   branch_analysis.duckdb
data/outputs/     Power BI extracts, Excel model, QA reports
scripts/          numbered pipeline
sql/              one query per file, keyed to a business question
config/           index weights, catchment definition
docs/             data dictionary, quality log, assumptions, UAT log
```

## Method in brief

Branches are geocoded to census tracts by spatial join. Each branch is assigned
a catchment of tracts within a documented straight-line radius. Catchment
demographics predict expected deposits; the gap between expected and actual
identifies over- and under-performance. Unmet mortgage demand comes from
comparing the institution's originations against all lenders' originations in
the same tract — which requires bridging FDIC's CERT identifier to HMDA's LEI
through RSSD, as no direct key exists between the two datasets.

Markets are ranked by a weighted composite index whose weights live in
`config/index_weights.yaml`, are justified in the case study, and are
stress-tested against alternative weightings.

## Limitations

- **Deposits are booked where the account is opened, not where the customer
  lives.** Large corporate and brokered deposits concentrate at headquarters
  branches, inflating their apparent local performance. This is the single
  largest caveat on the branch performance index.
- **Catchments use straight-line distance, not drive time.** Rivers, highways,
  and lakes are invisible to this model — a real constraint in a Great Lakes
  footprint.
- **The public HMDA file contains no credit score**, and bins debt-to-income,
  age, loan amount, and property value. No finding here controls for
  underwriting factors, and none should be read as if it did.
- **Index weights are judgment calls.** The sensitivity analysis shows how much
  the ranking depends on them.

## Documentation

Design rationale, business requirements, KPI definitions, and the UAT log live
in `docs/`. `CLAUDE.md` carries working instructions for AI-assisted
development.
