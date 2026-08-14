# Branch Network Strategy — Wisconsin & Illinois

An outside-in analysis of a regional bank's branch network, built entirely from
publicly filed federal data, to answer: **where should the next three branches
go, and which existing locations warrant review?**

> This analysis uses only publicly available regulatory filings. It reflects no
> relationship with, or confidential knowledge of, any institution discussed.

## Data

| Source | Vintage | What it provides |
|---|---|---|
| FDIC Summary of Deposits | 2019–2025 | Deposits at every branch of every insured institution |
| HMDA Modified LAR | 2025 | Loan-level mortgage applications and outcomes by tract |
| HMDA Public Panel | 2025 | Institution identifiers (the LEI ↔ RSSD bridge) |
| FDIC Institutions | current | Institution attributes including RSSD |
| ACS 5-year | 2020–2024 | Tract demographics, income, housing |
| TIGER/Line | 2024 | Census tract geometry |

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

Downloads land in `data/raw/` alongside `manifest.json`, which records the
source URL, retrieval timestamp, byte size, and SHA-256 of every file.

**If a download fails**, the endpoint has likely moved. Several URLs in
`scripts/01_download.py` are tagged `VERIFY` with their landing pages. Confirm
the current pattern, correct it, and log the change in
`docs/data_quality_log.md`. The script refuses to save an HTML landing page as
data — a silent wrong download is worse than a loud failure.

## Running

Scripts are numbered and idempotent; run in order. A clean re-run from `raw/`
must reproduce identical outputs (acceptance criterion AC-04).

**Windows (PowerShell)**

```powershell
Get-ChildItem scripts\[0-9]*.py | Sort-Object Name | ForEach-Object {
    python $_.FullName
    if ($LASTEXITCODE -ne 0) { Write-Host "Stopped at $($_.Name)"; break }
}
```

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
