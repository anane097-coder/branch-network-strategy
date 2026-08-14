# Project Guide — Branch Network Strategy Analysis

The working reference for this repository: scope, rules, data model, and the
traps that matter. Read this before starting any stage.

---

## What this project is

A **portfolio case study** demonstrating end-to-end Data Analyst and Business Analyst work: an outside-in branch network analysis of a real regional bank in Wisconsin and Illinois, using only publicly filed federal data.

The audience is a **hiring manager**, not a production system. That single fact drives every rule below. Optimize for work that is legible, documented, defensible, and finishable — not for engineering elegance.

**Business framing:** a WI-headquartered regional bank has capital for three new branches and must identify two underperforming locations for review. Deliverables are a recommendation with evidence *and* a repeatable tool the retail team can re-run against a new data vintage.

---

## Scope rules

**Out of scope, deliberately — regardless of how much time is available:**
- No machine learning. No scikit-learn, no predictive models. The deliverable is a weighted index with defended weights — that is a deliberate choice, not a limitation.
- No drive-time isochrones or gravity models. Catchments use a straight-line radius, documented as an assumption.
- No cloud warehouse. DuckDB, local.
- No generalized ETL framework, no abstraction layers "for later." Scripts run in numbered order.
- No scope beyond Wisconsin and Illinois.
- Never edit anything under `data/raw/`. It is immutable.

**Always:**
- Verify field names against the source's own documentation before writing code that depends on them. Do not trust field names from memory or from this file — endpoints and schemas have already changed once during this project.
- When a data-quality problem appears, **log it in `docs/data_quality_log.md` and raise it.** Do not silently fix, drop, or impute. The log is a portfolio deliverable; problems found are evidence of rigor, not failure.
- Write every SQL query so it maps to a business question ID (BQ-1 … BQ-6). Put the ID in a comment at the top of the file.
- State assumptions in code comments *and* in `docs/assumptions.md`.

**Needs an explicit decision before proceeding:**
- Dropping any rows, for any reason.
- Changing the catchment radius, the index weights, or the year range.
- Adding a dependency not in `requirements.txt`.
- Anything that would take more than ~90 minutes.

---

## Data sources and vintages

| Source | Vintage | Grain | Key |
|---|---|---|---|
| FDIC Summary of Deposits (REST API — the bulk ZIP is dead) | 2019–2025 (14 state-year files) | Branch × year | UNINUMBR |
| HMDA loan-level, Data Browser (**snapshot** filtered by state — *not* the Modified LAR, despite what the design docs say) | 2025 | Loan application | LEI |
| HMDA filer list (all that remains of the panel — **carries no RSSD**) | 2025 | Institution | LEI |
| FDIC Institutions (all states, not just WI+IL) | current | Institution | CERT, FED_RSSD |
| ACS 5-year — tract **and CBSA** level | 2020–2024 | Census tract / CBSA | 11-digit GEOID / CBSA code |
| CBSA delineation (county → CBSA) | 2023 | County | 5-digit county FIPS |
| TIGER/Line tracts | 2024 | Census tract | GEOID |

**Before touching acquisition or the crosswalk, read `docs/data_quality_log.md`.** Several endpoints assumed by the design docs no longer exist, and the RSSD → LEI bridge currently has no automated source at all.

**Geography spine: the 11-digit census tract GEOID** (state 2 + county 3 + tract 6). Store as TEXT, never as an integer — leading zeros are significant and losing them is the single most common bug in this kind of work.

### The entity-resolution problem — read this before touching HMDA

FDIC keys institutions on CERT. HMDA keys them on LEI. There is no shared key. The designed bridge ran CERT → FED_RSSD (FDIC institutions API) → RSSD → LEI (HMDA public panel).

**As of 2026-08-14 the second half of that bridge has no working source.** The panel ZIP returns S3 `AccessDenied`, the Federal Reserve NPW bulk download is CAPTCHA-walled, and the FFIEC institutions API returns `rssd: -1` for every institution tested. `FED_RSSD` from FDIC is fine; there is nothing to join it *to*. Do not paper over this with name matching without saying so loudly — an unflagged fuzzy match between two federal datasets is exactly the kind of silent error this project is meant to demonstrate avoiding. Any name- or tax-ID-based match must carry `match_method` and `match_quality` and be manually verified for the subject bank.

**Decided 2026-08-14 — build the crosswalk for a shortlist, by hand, and verify it.** Do not attempt a programmatic match across all 622 certs. Script 04 builds `dim_institution_crosswalk` only for the institutions that survive selection criteria 1–3 (roughly 20), matching on name + city + tax ID, with `match_method` and `match_quality` populated on **every** row and the subject bank's match verified by hand against both agencies' public records. A set that small can be defended line by line in an interview; hundreds of unverifiable fuzzy matches cannot.

Write up the disappearance itself. "The two datasets shared no key, and partway through the project the bridge dataset was withdrawn, so I built a smaller one and verified it by hand" is a better story than the one originally planned, and it is true.

If the subject bank cannot be resolved to an LEI present in the 2025 data, **stop and reassess the subject institution** — BQ-4 depends on it and a different institution may be required.

**Public HMDA file constraints.** The public file has no credit score at all. DTI, applicant age, loan amount, and property value are binned or rounded to midpoints. Any statement about lending outcomes must be written to respect this. Never phrase a finding as if underwriting factors were controlled for — they cannot be, with this file.

---

## Reference documents

Read these when the relevant work starts. They contain reasoning this file only summarizes.

| File | Read before |
|---|---|
| `docs/08-project-a-design.md` | Any analysis work. Full design: stakeholders, requirements, KPI formulas, all 14 SQL tasks, dashboard spec, UAT plan. |
| `docs/05-risks-and-data-access.md` | Data acquisition and any HMDA work. Access mechanics and the ethical guardrails on tract-level lending analysis. |

Scope decisions, build sequencing, and stage gates are summarized in this file — the institution selection criteria, script sequence, and acceptance criteria below are the authoritative statements of each.

Living documents to update as the work proceeds — these are portfolio deliverables, not scratch files:
`docs/data_quality_log.md` · `docs/assumptions.md` · `docs/data_dictionary.md` · `docs/uat_log.md`

---

## Repository layout

```
data/raw/           immutable downloads + manifest.json (never edit)
data/staging/       typed, renamed, filtered to WI+IL
data/warehouse/     branch_analysis.duckdb
data/outputs/       Power BI extracts, Excel model, QA reports
scripts/            numbered, run in order
sql/                one file per query, named by ID
config/             index_weights.yaml, catchment.yaml
docs/               design, data dictionary, quality log, assumptions, UAT log
notebooks/          exploration only — nothing load-bearing lives here
```

Scripts are numbered and idempotent. Re-running from scratch must reproduce identical outputs — that is acceptance criterion AC-04 and it will be tested.

---

## Data model

**Dimensions**
- `dim_institution` — cert, name, hq_state, assets, fed_rssd, is_subject_bank
- `dim_institution_crosswalk` — cert, fed_rssd, lei, match_quality, match_method
- `dim_branch` — uninumbr (PK — FDIC's unique office number; a per-institution branch number collides across institutions, see A-06), cert, name, address, city, county_fips, latitude, longitude, tract_geoid (derived via spatial join), service_type, first_year, last_year
- `dim_tract` — tract_geoid (PK), county_fips, county_name, cbsa, households, median_hh_income, median_home_value, owner_occupied_units, lmi_flag, centroid_lat, centroid_lon
- `dim_year` — year, sod_as_of_date

**Facts**
- `fact_branch_deposits` — uninumbr × year → deposits, institution_total_deposits
- `fact_tract_lending` — tract_geoid × lei × loan_purpose × action_taken → application_count, origination_count, origination_amount, denial_count
- `bridge_branch_catchment` — uninumbr × tract_geoid → distance_miles, is_primary

HMDA is pre-aggregated to `fact_tract_lending` in staging. Loan-level rows never enter the warehouse.

---

## Business questions

| ID | Question |
|---|---|
| BQ-1 | Which markets have the strongest deposit opportunity relative to competitor saturation? |
| BQ-2 | Where is our deposit share declining fastest, and is that market-wide or branch-specific? |
| BQ-3 | Which branches underperform relative to what their catchment demographics predict? |
| BQ-4 | What mortgage demand exists in our footprint that we are not capturing? |
| BQ-5 | Where would three new branches sit, and what is the fiscal case? |
| BQ-6 | Does the recommended set improve or worsen coverage of low- and moderate-income tracts? |

**BQ-6 is not optional and not an appendix.** An index weighted toward income and home value will systematically favor affluent tracts. The equity check measures that and reports it honestly, including when the answer is unflattering. If the recommended set worsens LMI coverage, say so in the output and show the alternative. Frame unmet mortgage demand as *underserved demand the bank is missing* — that is both the more defensible framing and the more commercially interesting one.

---

## Script sequence

| Script | Does | Exit condition |
|---|---|---|
| `01_download.py` | Fetch all sources, write manifest with SHA-256 | All files present, manifest written |
| `02_profile_sod.py` | Load 7 SOD vintages, profile drift, build column mapping | Profile written (see quality log — this gate needs revising) |
| `03_select_institution.py` | Apply selection criteria, profile candidates | Subject chosen, rationale in `docs/institution_selection.md` |
| `04_crosswalk.py` | CERT ↔ RSSD ↔ LEI | Subject's LEI confirmed in 2025 data (UAT-10) |
| `05_stage_acs.py` | ACS API pull, tract attributes | Staging table complete |
| `06_stage_hmda.py` | Filter WI/IL, aggregate to tract grain | `fact_tract_lending` staged |
| `07_spatial_join.py` | Branch coordinates → tract polygons | Zero unmatched, or all exceptions enumerated |
| `08_catchments.py` | Distance matrix, radius rule, bridge table | Every tract in ≤1 primary catchment (AC-03) |
| `09_build_warehouse.py` | Load DuckDB dimensional model | All tables present, row counts logged |
| `10_index.py` | Composite index from `config/index_weights.yaml` | Index computed, reproducible |
| `11_sensitivity.py` | N alternative weightings, ranking stability | Stability report written |
| `12_qa_report.py` | Reconciliation, nulls, unmatched lists | AC-01 reconciliation within 0.1% |

## Institution selection criteria (script 03)

1. Branches in both WI and IL
2. ≥25 branches across WI+IL
3. Present in all seven SOD vintages (no mid-period merger disappearance)
4. Active 2025 HMDA filer with a resolvable LEI
5. Not a top-5 national bank (deposits book to HQ at scale and distort branch figures)
6. Assets roughly $2B–$60B
7. Not in a publicly announced merger

Write the shortlist and the reasoning to `docs/institution_selection.md`. That document is itself a work sample — it should read like an analyst explaining a choice, not like a log file.

---

## Acceptance criteria (tested in UAT)

- **AC-01** — Branch deposits summed by state match FDIC published state totals within 0.1%
- **AC-02** — Every branch resolves to exactly one tract, or unmatched branches are enumerated with reasons
- **AC-03** — Every tract appears in at most one primary catchment
- **AC-04** — Clean re-run from raw produces identical index values
- **AC-05** — Setting any weight to zero changes the ranking (proves the component is wired in)
- **AC-06** — LMI coverage rate computed for both current and recommended footprints
- **UAT-10** — Subject institution resolves to exactly one LEI present in the 2025 data

---

## Style

- Python: standard library plus the pinned dependencies. Type hints on function signatures. No classes unless there is a clear reason.
- SQL: CTEs over nested subqueries. Uppercase keywords. One query per file in `sql/`, named `SQL-04_rank_branches_by_growth.sql`.
- Tract GEOIDs, county FIPS, and CERT numbers are **TEXT**. Always.
- Money in whole dollars in the warehouse; note that SOD reports deposits in thousands and convert explicitly at staging with a comment.
- Every script prints a short summary of what it did and how many rows it touched.

## Known traps

1. **Leading zeros.** Any FIPS or GEOID read as a number is a silent, destructive bug. Confirmed live: the SOD API returns `STNUMBR` and `CNTYNUMB` as integers, and 859 of 1,295 Wisconsin county codes need zero-padding.
2. **SOD reports deposits in thousands.** Convert once, at staging, explicitly.
3. **Drift across the seven SOD vintages.** Less of a problem than expected now the REST API normalizes field names — but content drift is real and unexamined.
4. **Branches disappear mid-period** through closure or merger. Handle deliberately — survivor bias is a finding, not a nuisance.
5. **Tract vintage mismatch.** ACS 2020–2024 uses 2020 boundaries. ACS and TIGER 2024 align exactly at 4,807 tracts, but one HMDA tract is absent from TIGER — resolve it before the spatial join.
6. **Census suppression sentinels.** ACS returns large negative values (e.g. `-666666666`), not nulls, for suppressed estimates. Read as a number, that becomes a negative income feeding the index.
7. **Deposits are booked where the account is opened**, not where the customer lives. Large corporate and brokered deposits concentrate at headquarters branches. This is the project's headline limitation and belongs in the executive summary, not buried.
