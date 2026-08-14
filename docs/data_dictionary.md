# Data Dictionary

Populated as tables are built. Every field in the warehouse appears here.

## dim_branch

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| uninumbr | TEXT | FDIC SOD | Unique office number — the primary key | FDIC's stable branch identifier. **Persists across a change of ownership**, which is what lets script 02 separate transfers from openings and closings. The per-institution "branch number" is unique only within an institution and fails as a PK (A-06) |
| cert | TEXT | FDIC SOD | FDIC certificate number | TEXT — leading zeros matter |
| county_fips | TEXT | derived | 5-digit county FIPS | Assembled as `zfill(2) + zfill(3)` from `STNUMBR` and `CNTYNUMB`, which the API returns as **integers**. 859 of 1,295 WI county codes lose a digit without this |
| tract_geoid | TEXT | derived | 11-digit census tract | Spatial join, script 07 |
| state | TEXT | FDIC SOD `STALPBR` | State the branch is **physically in** | **Not `STALP`**, which is the institution's charter state. Filtering on the wrong one produces a plausible extract of the wrong population — see the quality log |

## dim_institution

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| cert | TEXT | FDIC | Certificate number, PK | |
| fed_rssd | TEXT | FDIC | Federal Reserve identifier | The bridge to HMDA. Joins to FFIEC `institutionId2017` |
| attribute_as_of_date | DATE | derived | Vintage of the attributes on this row | **Required.** Institution attributes are current-vintage; branch and deposit data are as of 30 June of the SOD year. For the subject these straddle a completed acquisition (Associated closed American National 2026-04-01), so any ratio with an institution-level denominator over a branch-level numerator spans the gap unless forced to confront it |

## dim_institution_crosswalk

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| lei | TEXT | HMDA | Legal Entity Identifier | Resolved via `FED_RSSD == institutionId2017`. **Never keyed on CERT** — the 2017 ARID's respondent ID meant cert, charter number, or RSSD depending on regulator, so a CERT join mis-resolves national banks and looks clean |
| match_quality | TEXT | derived | Why the row did or did not resolve | `exact_rssd` · `no_lei_for_rssd` · `no_fed_rssd` · `not_in_fdic`. Never null — an unresolvable institution is a finding, not a gap |
| match_method | TEXT | derived | How it resolved | `fed_rssd == institutionId2017`, or blank |

## dim_tract

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| tract_geoid | TEXT | ACS / TIGER | 11-digit GEOID, PK | ACS and TIGER 2024 align exactly at 4,807 tracts, zero orphans either way |
| tier | TEXT | derived | `metro` · `micro` · `rural` | Three tiers, not a binary urban split. Micropolitan branch spacing is 3.7× metro, so the binary rule understated those catchments roughly fourfold (A-01b) |
| households | INTEGER | ACS B25003_001E | Occupied housing units | Any negative value is a suppression sentinel, not a count — see `tract_status` |
| median_family_income | INTEGER | ACS B19113_001E | Median **family** income | Family, not household. The FFIEC LMI definition is family-based (A-03) |
| tract_to_area_income_pct | REAL | HMDA / derived | Tract income as % of area income | Basis recorded in `lmi_basis` |
| lmi_basis | TEXT | derived | Which source produced the ratio | `ffiec_ratio` (4,772) — the published figure examiners use · `acs_fallback` (4) — reconstructed for tracts with no lending activity, a different basis and labelled as one · empty (31) — no basis by either route, the AC-06 exceptions |
| lmi_flag | BOOLEAN (nullable) | derived | Ratio < 80 | **Nullable.** A tract with no income basis is neither LMI nor non-LMI. Collapsing unknown into `false` would inflate the non-LMI count and quietly improve the BQ-6 equity result |
| tract_status | TEXT | derived | Why a value is missing | Three distinct states — see below |
| centroid_lat / centroid_lon | REAL | TIGER | Tract centroid | Computed in EPSG:5070, returned as WGS84 |

### `tract_status` — missingness is three things

A suppressed estimate, an unpopulated tract, and a water polygon all arrive as
"no value" and are not the same fact. Each has its own handling rule.

| Status | Count | Meaning | Handling |
|---|---|---|---|
| `ok` | 4,692 | Values present | Used normally |
| `suppressed` | 84 | Census withheld the estimate; the sample was too small. A real, inhabited tract whose value is unknown | Excluded from averages. **Counted** in denominators meaning "tracts that exist" |
| `water_or_special` | 28 | Tract codes `99xx` (water) and `98xx` (special land use). Not a residential geography | Excluded entirely. **Never counted as a coverage gap** — these are already enumerated on the AC-06 exception list and must not be re-admitted as a third flavour of missing |
| `unpopulated` | 3 | Zero households. Nothing withheld; nothing to report | Excluded from averages **and** from rate denominators |

**Suppression sentinels.** Census encodes suppression as large negative
integers. Only `-666666666` occurs in this vintage (median household income
54 tracts, median family income 114, median home value 101) — but the pipeline
filters on **sign**, not on that value, because the sentinel set is not fixed
and a different code would otherwise be admitted as a real negative income.
Which codes appeared is logged rather than discarded: the code carries the
reason.

## fact_branch_deposits

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| uninumbr | TEXT | FDIC SOD | Branch, FK to dim_branch | |
| deposits | BIGINT | FDIC SOD `DEPSUMBR` | Branch deposits, whole dollars | **SOD reports thousands** — converted once, at staging, explicitly |

## fact_tract_lending

**Grain: `tract_geoid` × `lei` × `loan_purpose` × `action_taken`.**

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| tract_geoid | TEXT | HMDA | 11-digit tract | |
| lei | TEXT | HMDA | Legal Entity Identifier | Bridges to cert via RSSD. Lenders that do not resolve still count toward tract totals; they simply cannot be named |
| loan_purpose | TEXT | HMDA | Purpose code | `1` Home purchase · `2` Home improvement · `31` Refinancing · `32` Cash-out refinancing · `4` Other · `5` Not applicable |
| action_taken | TEXT | HMDA | Outcome code — **part of the grain, deliberately** | See mapping below. Kept in the grain so no query can count originations without naming `action_taken = 1`; a pre-filtered fact table would hide that choice inside staging where no reviewer sees it |
| record_count | INTEGER | derived | Rows at this grain | Deliberately minimal. Every meaningful ratio is defined in SQL against explicit codes |
| total_amount | BIGINT | derived | Sum of `loan_amount` | HMDA rounds amounts to midpoints — see A-03 constraints on the public file |

### `action_taken` — code to meaning

| Code | Meaning | Class | Share |
|---|---|---|---|
| 1 | Loan originated | origination | 55.7% |
| 2 | Application approved but not accepted | application | 2.9% |
| 3 | Application denied | application | 14.1% |
| 4 | Application withdrawn by applicant | application | 12.2% |
| 5 | File closed for incompleteness | application | 4.4% |
| **6** | **Purchased loan — bought, NOT originated** | purchased | 10.5% |
| 7 | Preapproval request denied | preapproval | 0.1% |
| 8 | Preapproval request approved but not accepted | preapproval | 0.2% |

**Originations are `action_taken = 1` only.** Including code 6 overstates
lending by 18.9%; no filter at all overstates it by 81.0%. The distortion is
uneven — Associated is 0.8% purchased while several lenders exceed 88% — so
including it would inflate competitors far more than the subject.

### ⚠ Known bias: tract-level denial rates read low

**Any tract-level denial-rate measure built on this table carries a known
downward bias.** 1.9% of denials are geographically unattributable against
0.3% of originations, so denials are systematically under-represented at tract
grain by roughly a factor of six.

The mechanism is not mysterious: applications denied early — incomplete files,
credit-based denials reached before a property is identified — may never
acquire a property address to geocode. It is the same reason preapprovals
dominate the untracted set (50.3% of code-7 and 71.6% of code-8 records lack a
tract).

This is recorded here, against the table, rather than only in prose, because a
prose caveat separates from the number it qualifies the moment someone copies
a chart into a deck. See assumption **A-07**.

### ⚠ Known gap: three lenders report no geography at all

Three LEIs report a null census tract on **100% of their records** (751, 706,
and 278 records; 1,735 combined). They are invisible to any tract-level
competitive analysis, which touches **BQ-1 competitor saturation** and **BQ-4
unmet demand**.

**Direction of bias matters here:** a market missing lenders looks *less*
competitive than it is, which biases toward recommending expansion into it.
Small in volume, but pointed the wrong way for the decision this project
exists to make.
