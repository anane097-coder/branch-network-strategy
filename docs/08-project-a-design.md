# 08 — Project A Design: Branch Network Strategy

**Phase 4 deliverable. No implementation until this is reviewed.**

> **Partially superseded.** Later scope decisions overrode several statements in
> this document's body, not just its open questions. Where they conflict, the
> resolutions below win:
> - §4 "Pull ~5 years for trend" and the `fdic_sod_2021_2025.csv` example in §5 →
>   **seven vintages, 2019–2025**. The longer window spans the post-2020 branch
>   consolidation, which lets the analysis ask whether the subject bank is
>   consolidating faster or slower than its market — a question a three-year
>   window cannot answer.
> - §0's open question on real vs. composite institution → **real, named,
>   analyzed outside-in from public filings**, with the §0 guardrails applied.
> - §6 "branch number (PK)" → the primary key is **`UNINUMBR`**, FDIC's unique
>   office number. A per-institution branch number collides across institutions
>   (assumption A-06).
>
> Several endpoints and one whole data source assumed by this design have since
> changed. Read `docs/data_quality_log.md` before implementing any of it.

---

## 0. One decision to make before anything else

`04` leaned toward a fictional composite bank. Having designed the data model, **I've changed my mind and now recommend using a real, named institution**, analyzed outside-in from public data only.

| | Composite bank | Real named bank (recommended) |
|---|---|---|
| Data | Branches must be invented or borrowed and relabeled | Every branch is real, in FDIC's file, with real deposits and real history |
| Credibility | Interviewer can't verify anything | Interviewer can check the numbers |
| Awkwardness | "Why is your data fake?" | Must be careful with closure language |
| Reconciliation to FDIC totals | Meaningless | Works, and is one of the strongest UAT items |

The composite version quietly destroys the best validation story in the project. An outside-in analysis of a real institution using only public filings is a normal, legitimate exercise — it's what competitors, consultants, and equity analysts do routinely.

**Guardrails if we go this way:** state on page one that the analysis uses only publicly filed data and that the candidate has no relationship with or confidential knowledge of the institution. Frame closures as *"branches performing furthest below what their catchment supports — candidates for review"*, never as "close these." Growth recommendations can be stated directly.

**Candidate subjects** (Wisconsin-headquartered, WI+IL footprint): Associated Banc-Corp, Johnson Financial Group, First Business Financial, Waukesha State Bank. Don't take my word on current size or branch count for any of them — the SOD file answers that in Week 1, and letting the data pick the subject is a better story than picking first.

---

## 1. Business scenario

A Wisconsin-headquartered regional bank has seen flat deposit growth for three consecutive years while its footprint states show uneven population and lending activity. The board has approved capital for **three new branch locations** and directed the retail team to identify **two underperforming locations for review**.

The analyst is asked for two things: a recommendation with evidence, and a **repeatable tool** the retail team can re-run when the next SOD vintage lands. The second half matters — it's the difference between a report and a product, and it's what generates the requirements and UAT artifacts.

## 2. Stakeholders

| Stakeholder | Role | Interest | Involvement |
|---|---|---|---|
| Chief Strategy Officer | Sponsor | Deploy capital where returns are strongest | Approves criteria and final recommendation |
| Retail Banking Director | Primary user | Owns branch P&L; will operate the dashboard | Defines filters and drill paths; signs UAT |
| CRA / Compliance Officer | Reviewer, gatekeeper | Ensures the siting model doesn't produce a disparate footprint | Reviews index weights and the equity check; can veto |
| Finance / FP&A | Contributor | Cost model, breakeven assumptions | Supplies branch cost inputs and validates the fiscal case |
| Market Research Lead | Contributor | Owns competitor intelligence | Reviews market definitions |
| Data / IT | Supplier | Data delivery and refresh | Owns the pipeline hand-off |

The Compliance Officer is not decoration. Making a compliance stakeholder a *gatekeeper with veto power* over an expansion model is exactly how this works at a real bank, and it's what forces the equity check into the acceptance criteria rather than into a footnote.

## 3. Business questions

| ID | Question | Primary output |
|---|---|---|
| BQ-1 | Which markets in our footprint have the strongest deposit opportunity relative to competitor saturation? | Ranked market list |
| BQ-2 | Where is our deposit share declining fastest, and is that a market-wide trend or specific to our branches? | Market vs. branch attribution |
| BQ-3 | Which of our branches underperform relative to what their catchment demographics predict? | Branch performance index |
| BQ-4 | What mortgage demand exists in our footprint that we are not capturing? | Unmet demand by tract |
| BQ-5 | Where would three new branches sit, and what is the fiscal case? | Recommendation + model |
| BQ-6 | Does the recommended set improve or worsen our service coverage of low- and moderate-income tracts? | Equity check |

BQ-6 is a first-class question, not an appendix. It is also the one most likely to generate a genuinely interesting interview conversation.

## 4. Data sources

| Source | Grain | Key fields | Notes |
|---|---|---|---|
| **FDIC Summary of Deposits** | Branch × year | Institution cert, unique branch number, branch name/address, state-county FIPS, latitude/longitude, branch deposits, service type, main-office flag | Confirm exact field names against the SOD documentation at load — don't code from memory. Pull ~5 years for trend. |
| **HMDA Modified LAR 2025** | Loan application | Activity year, LEI, state/county/tract, action taken, loan purpose, loan type, loan amount, income, denial reasons, tract income ratio, tract minority population % | **Modified file: no credit score at all; DTI, age, loan amount, and property value are binned or rounded.** This constrains what can be claimed. |
| **ACS 2020–2024 5-year** | Census tract | Population (B01003), median household income (B19013), tenure (B25003), median home value (B25077), income distribution (B19001) | API key required. |
| **TIGER/Line tract shapefiles** | Census tract | GEOID, geometry | Must match the ACS vintage — 2020 boundaries. |
| **County Business Patterns** | County or ZIP × NAICS | Establishment counts by size | **Stretch goal only.** Confirm current published vintage before designing it in. |

**Geography spine:** the 11-digit census tract GEOID (state 2 + county 3 + tract 6). HMDA provides it directly. ACS provides it directly. **FDIC does not** — SOD gives coordinates and county, so branches reach tract level only through a spatial join. That's the one real engineering step in this project.

## 5. Data architecture

```
raw/                    exactly as downloaded, never edited, vintage in filename
  fdic_sod_2021_2025.csv
  hmda_lar_2025_wi_il.csv
  acs5_2024_tract_wi_il.csv
  tl_2024_tract_wi_il.shp

staging/                typed, renamed, filtered to WI+IL
transform/              Python: spatial join, catchment assignment, index
warehouse/              DuckDB: dimensional model below
outputs/                Power BI extracts, Excel model, QA reports
```

Raw files are committed (or a documented subset is, if size forces it). This is the `05` §1 durability point — the pipeline must survive a source going away.

## 6. Data model

**Dimensions**
- `dim_institution` — cert, name, headquarters state, total assets, `is_subject_bank` flag
- `dim_branch` — branch number (PK), cert (FK), name, address, city, county FIPS, latitude, longitude, **tract GEOID (derived)**, service type, first and last year observed
- `dim_tract` — tract GEOID (PK), county, county name, CBSA, ACS attributes, LMI flag, centroid lat/long
- `dim_year` — year, SOD as-of date

**Facts**
- `fact_branch_deposits` — grain: branch × year. Deposits, institution total deposits that year, derived market share.
- `fact_tract_lending` — grain: tract × lender × loan purpose × action taken. Application count, origination count, total origination amount, denial count. Pre-aggregated from the LAR; do not carry loan-level rows into the warehouse.
- `bridge_branch_catchment` — grain: branch × tract. Distance in miles, catchment flag. Resolves the many-to-many between branches and tracts.

**Catchment definition (an assumption that must be stated loudly):** a tract belongs to a branch's catchment if the tract centroid falls within **3 miles** of the branch in urbanized areas or **8 miles** elsewhere. Straight-line, not drive-time.

This is deliberately crude. Drive-time isochrones and gravity models are the rabbit hole flagged in `05` §5. A documented radius assumption with a stated limitation is what many real teams use, and *defending a simple assumption well is a stronger analyst signal than implementing a complex one badly.*

## 7. KPI definitions

| KPI | Formula | Grain | Owner |
|---|---|---|---|
| Branch deposits | SOD reported deposits | Branch × year | Retail Director |
| Deposit CAGR (3yr) | (Deposits_t / Deposits_t-3)^(1/3) − 1 | Branch, market | Retail Director |
| Market deposit share | Our deposits in county ÷ all-institution deposits in county | County × year | Strategy |
| Deposits per branch vs. market median | Branch deposits ÷ median branch deposits in same county | Branch × year | Retail Director |
| Competitor branch density | Competitor branches ÷ households in catchment × 10,000 | Branch catchment | Strategy |
| Catchment potential | Households × median income, summed over catchment tracts | Branch catchment | Strategy |
| Branch performance index | Actual deposits ÷ deposits predicted from catchment potential | Branch | Retail Director |
| Unmet mortgage demand | Originations by all lenders in tract − our originations | Tract × year | Strategy |
| Market opportunity score | Weighted composite (§8) | Tract, market | Strategy + Compliance |
| LMI coverage rate | Share of LMI tracts within any of our catchments | Footprint | Compliance |

Every KPI gets a written definition, a formula, an owner, and a stated assumption in the case study. This section alone is more BA rigor than most portfolios contain.

## 8. The opportunity index

Starting weights, to be justified and sensitivity-tested — **not** presented as objectively correct:

| Component | Weight | Direction |
|---|---|---|
| Household growth in catchment | 20% | higher better |
| Median household income | 15% | higher better |
| Deposit market growth (county CAGR) | 25% | higher better |
| Competitor saturation | 20% | lower better |
| Unmet mortgage demand | 20% | higher better |

Requirements on the index: components normalized (min-max or z-score, decide and document); weights held in a config file, not hardcoded; sensitivity analysis showing how the top-10 ranking shifts under alternative weightings; **and an equity check on the final recommended set**.

On BQ-6: if income and home value dominate, the model will systematically favor affluent tracts. That is the redlining risk in `05` §4 — and the honest handling is to measure it, report it, and if the recommended set worsens LMI coverage, say so and show the alternative. Frame unmet mortgage demand as *underserved demand the bank is missing*, which is both more defensible and more commercially interesting.

## 9. Requirements

**Business requirements**
- BR-01 — Rank all markets in the footprint by opportunity using consistent, documented criteria.
- BR-02 — Identify branches performing below catchment potential.
- BR-03 — Recommend three expansion locations with supporting evidence.
- BR-04 — Provide a fiscal case for each recommendation.
- BR-05 — Ensure recommendations are assessed for LMI coverage impact before approval.
- BR-06 — Deliver a tool the retail team can re-run against a new SOD vintage without analyst involvement.

**Functional requirements**
- FR-01 — Dashboard filters by state, county, CBSA, and institution.
- FR-02 — Drill path: market map → county → branch detail → catchment tracts.
- FR-03 — Index weights adjustable without rebuilding the model.
- FR-04 — Every KPI displays its definition on hover or in an adjacent glossary page.
- FR-05 — Data refresh documented as a runnable, ordered procedure.
- FR-06 — Equity check appears as a visible dashboard page, not a hidden calculation.

**Acceptance criteria (samples — full set in the UAT log)**
- AC-01 — Sum of branch deposits by state equals FDIC's published state totals within 0.1%.
- AC-02 — Every branch resolves to exactly one tract; zero unmatched branches, or unmatched branches are enumerated with reasons.
- AC-03 — Every tract in the analysis appears in at most one primary catchment.
- AC-04 — Index recomputes to identical values on a clean re-run from raw.
- AC-05 — Changing any weight to zero produces a ranking change (proves the component is actually wired in).
- AC-06 — LMI coverage rate is computed for both current and recommended footprints.

## 10. SQL task list

Every query maps to a business question and exercises a named technique. This is the file to open in a technical interview.

| ID | Query | Technique | BQ |
|---|---|---|---|
| SQL-01 | Deposits by county by year, all institutions | GROUP BY, date filtering | BQ-1 |
| SQL-02 | Our market share by county by year | Aggregation with subquery denominator | BQ-1 |
| SQL-03 | Branch deposit CAGR over 3 years | `LAG`, self-join alternative | BQ-2 |
| SQL-04 | Rank branches by deposit growth within CBSA | `RANK() OVER (PARTITION BY ...)` | BQ-2 |
| SQL-05 | Market-vs-branch attribution: branch growth minus county growth | Window function + CTE chain | BQ-2 |
| SQL-06 | Branch percentile within county by deposits | `PERCENT_RANK()` | BQ-3 |
| SQL-07 | Catchment aggregation of tract demographics | Join through bridge, SUM | BQ-3 |
| SQL-08 | Branch performance index (actual vs. predicted) | CTEs, division with null handling | BQ-3 |
| SQL-09 | Origination and denial counts by tract and lender | `CASE` on action taken, GROUP BY | BQ-4 |
| SQL-10 | Our share of tract originations vs. all lenders | Aggregation, share calculation | BQ-4 |
| SQL-11 | Unmet demand ranking, top tracts not in any catchment | `NOT EXISTS`, ordering | BQ-4, BQ-5 |
| SQL-12 | Composite index assembly | Multi-CTE, normalization | BQ-5 |
| SQL-13 | LMI tract coverage, current vs. recommended | `CASE`, conditional aggregation | BQ-6 |
| SQL-14 | Reconciliation: computed state totals vs. FDIC published | Validation query | AC-01 |

## 11. Python task list

Python does what SQL can't, and nothing more.

- **PY-01** — ACS API extraction with pagination and key handling
- **PY-02** — HMDA filtering to WI/IL and pre-aggregation to tract grain
- **PY-03** — **Spatial join: branch coordinates → tract polygons** (geopandas point-in-polygon). The core engineering step.
- **PY-04** — Catchment construction: distance matrix branch × tract centroid, radius rule applied
- **PY-05** — Index computation with weights read from config
- **PY-06** — Sensitivity analysis: N alternative weightings, ranking stability report
- **PY-07** — QA report: row counts, null rates, reconciliation checks, unmatched-branch list

No modeling. No scikit-learn. See `03` — the index with defended weights is the stronger signal.

## 12. Power BI dashboard

Four pages. Each answers stated questions rather than displaying everything available.

**Page 1 — Market Opportunity.** Filled map of counties by opportunity score; top-10 market table; KPI cards (footprint deposits, 3yr CAGR, market share, branch count). Slicers: state, CBSA, minimum market size.

**Page 2 — Branch Performance.** Scatter of catchment potential vs. actual deposits with a fitted reference line — underperformers fall visibly below it. Table ranked by performance index. Drillthrough to Page 3.

**Page 3 — Branch Detail.** Single-branch view: deposit trend, catchment tract table, competitor branches within radius, mortgage capture rate.

**Page 4 — Recommendation and Equity Check.** The three proposed sites with supporting evidence; the review candidates; LMI coverage before/after; fiscal summary.

**DAX to write** (this is where the Power BI skill actually shows): deposit CAGR as a measure, market share with a correct denominator across filter context, performance index, LMI coverage rate, and a dynamic "selected weighting" measure driven by a what-if parameter. What-if parameters are the feature that turns FR-03 from a claim into a demonstration.

## 13. Excel deliverable

Not an afterthought — Excel appears in nearly every posting sampled.

Scenario model: branch cost assumptions (buildout, staffing, occupancy), deposit ramp curve, breakeven month, NPV over 7 years, and a two-variable data table on ramp rate × cost. Named ranges, an inputs sheet separated from calculations, and no hardcoded constants inside formulas.

## 14. UAT plan

| ID | Requirement | Method | Expected | Result |
|---|---|---|---|---|
| UAT-01 | AC-01 | Run SQL-14, compare to FDIC published state totals | Within 0.1% | |
| UAT-02 | AC-02 | Count branches with null tract GEOID | Zero, or enumerated with reasons | |
| UAT-03 | AC-03 | Count tracts appearing in multiple primary catchments | Zero | |
| UAT-04 | AC-04 | Full clean re-run from raw; diff index outputs | Identical | |
| UAT-05 | AC-05 | Set each weight to zero in turn; observe ranking | Ranking changes each time | |
| UAT-06 | FR-01 | Exercise every filter combination | No blank visuals, no errors | |
| UAT-07 | FR-02 | Walk the full drill path | Context carries correctly at each level | |
| UAT-08 | AC-06 | Compute LMI coverage current vs. recommended | Both values present and explained | |
| UAT-09 | FR-05 | Hand the refresh procedure to a naive follower | Completes without analyst help | |

UAT-09 is the one worth actually doing with a real person. "I wrote a refresh procedure, handed it to someone else, and watched where they got stuck" is a story that lands in interviews.

## 15. Executive summary structure

Four pages, written for someone who will read one: the decision, the three recommended markets with one-line rationale each, the review candidates, the fiscal case, the equity finding, the top three limitations, and what she'd do next with internal data.

**The limitation to lead with:** SOD reports deposits at the branch where an account is *booked*, not where the customer lives, and large corporate or brokered deposits can concentrate at headquarters branches. Naming that unprompted is a stronger competence signal than any chart in the deck.

---

## Open decisions

1. **Real named bank or composite?** I recommend real, outside-in (§0).
2. **Which institution**, if real — or do we let the SOD data pick in Week 1?
3. **WI+IL or WI only?** WI+IL is the better story; WI alone is the safer schedule.
4. **How many SOD years?** Five gives a credible trend; three is faster.
5. **Catchment radii** — are 3 and 8 miles sensible for this footprint, or should we look at the branch distribution first?
