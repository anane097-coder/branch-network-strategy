# 11 — Power BI Specification

Four pages. Supersedes `08` §12, which was written before the analysis reformulated four of five index components, split the index into raw and size-adjusted, and produced the constrained/commercial recommendation pair.

**Audience:** the Retail Banking Director operates it; the CRA Officer reviews page 4; a recruiter sees screenshots and a 45–60 second recording. Every page must survive being looked at for eight seconds.

**The discipline:** each visual answers a stated business question. If a chart doesn't map to BQ-1 through BQ-6, it doesn't ship. Twenty charts is the failure mode this design exists to avoid.

---

## Model to load

Import mode, from DuckDB extracts to CSV/Parquet in `data/outputs/`.

| Table | Grain | Rows | Role |
|---|---|---|---|
| `dim_tract` | tract | 4,807 | dimension |
| `dim_branch` | branch | 6,467 | dimension |
| `dim_institution` | institution | ~700 | dimension |
| `dim_year` | year | 7 | dimension |
| `fact_branch_deposits` | branch × year | 39,315 | fact |
| `fact_tract_lending` | tract × LEI × purpose × action | 474,707 | fact |
| `fact_tract_competition` | tract | 4,807 | fact |
| `bridge_branch_catchment` | branch × tract | 3,322 | bridge, subject only |
| `index_components` | tract | 4,733 scored | fact |
| `ref_index_weights` | component | 5 | reference |
| `recommendation_sets` | rule × site | 6 | fact |

**Relationships.** All single-direction, many-to-one, from fact to dimension. Do not enable bidirectional cross-filtering anywhere — with a bridge table present it produces ambiguous filter paths and silently wrong totals.

`bridge_branch_catchment` covers the subject only. Every measure that traverses it inherits that scope. Name such measures with a `Subject` prefix so a reader of the field list can see it.

**Types.** Tract GEOID, county FIPS, CERT, LEI all Text. Power Query will infer several as whole numbers on import — set them explicitly and check for leading-zero loss before building anything on top.

---

## DAX measures

Write these before any visual. Roughly twenty measures carry all four pages.

**Deposits and share**
```
Total Deposits = SUM(fact_branch_deposits[deposits])

Subject Deposits =
CALCULATE([Total Deposits], dim_institution[is_subject_bank] = TRUE)

Market Share =
DIVIDE([Subject Deposits], [Total Deposits])
```
`Market Share` must use `ALLEXCEPT` on geography if placed in a visual filtered by institution, or the denominator collapses to the subject. This is the classic filter-context error and it will look plausible.

**Trend**
```
Deposits PY =
CALCULATE([Total Deposits], DATEADD(dim_year[date], -1, YEAR))

Deposit CAGR 3yr =
VAR Current = [Total Deposits]
VAR Base = CALCULATE([Total Deposits], DATEADD(dim_year[date], -3, YEAR))
RETURN IF(Base > 0, (Current / Base) ^ (1/3) - 1, BLANK())
```
Return BLANK, never zero, when the base is missing — refuse before computing.

**Performance index**
```
Index (size-adjusted) = AVERAGE(dim_branch[index_size_adjusted])
Index (raw)           = AVERAGE(dim_branch[index_raw])
```
Both ship. The adjusted one is the default on every visual; raw is reachable through a field parameter.

**Opportunity, with live weighting** — see the what-if section below.

**Equity**
```
LMI Coverage = 
DIVIDE(
    CALCULATE(SUM(dim_tract[households]), dim_tract[lmi_flag] = TRUE, dim_tract[in_catchment] = TRUE),
    CALCULATE(SUM(dim_tract[households]), dim_tract[lmi_flag] = TRUE)
)
```
Households, never tracts. The 31 no-basis tracts are excluded from numerator and denominator by the `lmi_flag` NA handling — do not let a DAX filter collapse NA to FALSE.

---

## Page 1 — Market Opportunity

*Answers BQ-1 and surfaces the BQ-5 shortlist.*

**Header row — five cards:** footprint deposits (2025), branch count, 3-year deposit CAGR, market share, tract coverage %.

**Main visual — map.** A filled map of 4,807 tract polygons will be slow and illegible at national zoom. Use instead:
- County choropleth shaded by mean opportunity score (174 counties renders fast, reads clearly)
- Top-50 tracts as a point layer over it, sized by score
- Subject branches as a third layer, distinct marker

**Right rail — top-50 table.** Tract GEOID, county, opportunity score, and three composition flags as icons: LMI, cluster-measured growth, CBSA. The flags are the point — a reader should see at a glance that the shortlist is 4.0% LMI and 62% cluster-measured.

**Bottom — component contribution bar.** Each of the five components' contribution to top-50 selection. This is where `unmet_mortgage_demand` at 39.0% becomes visible.

**Slicers:** state, CBSA, tier (metro/micro/rural).

**Annotation, on-canvas, not a tooltip:** "Shortlist is 4.0% LMI against a 29.5% baseline; 62% of top-50 tracts carry estimated rather than observed growth."

---

## Page 2 — Branch Performance

*Answers BQ-3.*

**Main visual — scatter.** X: catchment potential (households × median income). Y: actual 2025 deposits. Point size: catchment households. Colour: diagnosis category (five states). A fitted reference line makes underperformers visually obvious below it.

**Field parameter — index basis.** A toggle between size-adjusted and raw, so the market-position effect stays visible rather than normalised out of sight. Default: size-adjusted.

**Diagnosis matrix.** Five categories × count, cross-filtering the scatter. Level and trajectory stay separate here — no composite rank.

**Ranked table.** Branch, market, index, CAGR, diagnosis, and four flag columns: `booking_concentration`, `catchment_partly_unmeasured`, `position_drift_miles`, `county_agrees`. Conditional formatting on the flags, not on the index — the flags are what a reader needs to spot.

**Drillthrough target:** page 3, on branch.

**Annotation:** "Brown County holds 18.6% of branches and 51.7% of deposits. Booking-concentration branches are flagged and excluded from the index."

---

## Page 3 — Branch Detail

*Drillthrough only. Not in the page navigator.*

Single branch, reached from page 2. Six elements:

1. **Header card** — branch name, address, market, tier, opening/closing years observed
2. **Deposit trend** — 2019–2025 line, with market median overlaid as a second series
3. **Index breakdown** — the branch's catchment potential, predicted deposits, actual, and the resulting index as a small waterfall or bullet
4. **Catchment tracts table** — GEOID, households, median income, LMI flag, distance, whether the tract was contested at assignment
5. **Competitor panel** — competitor branch count within tier radius, top three competing institutions by deposits in the same county
6. **Flags panel** — every flag that applies to this branch, in prose. If a branch carries `position_drift_miles` above threshold or `catchment_partly_unmeasured`, this is where a reader learns why its index reads as it does.

**Back button, top-left.** Drillthrough pages without one strand people.

---

## Page 4 — Recommendation and Equity Check

*Answers BQ-5 and BQ-6. The page that carries the finding.*

This page must present a tension, not a conclusion. Layout in three bands.

**Band 1 — the recommendation.** Map showing both site sets: Rule A (commercial) and Rule B (constrained), distinguishable at a glance, over the existing footprint. Bookmark toggle between them; both visible by default.

**Band 2 — the comparison table.** The centrepiece. Rows: current, Rule A, Rule B. Columns: LMI coverage, non-LMI coverage, Δ LMI, Δ non-LMI, binding test result, opportunity score, new catchment households.

The 8.4× ratio and the 0.93 pass must be legible without arithmetic. Conditional formatting on the binding test column: fail red, pass green.

**Band 3 — three text blocks, on canvas.**

1. **Cost of the constraint** — 0.6712 index points, 9.4% of unconstrained score, 26,617 new catchment households against 37,413.
2. **The 0-of-3 disclosure** — neither set places a branch in an LMI tract. Rule B improves catchment composition; it does not site among LMI households. Anyone reading this as a CRA response needs that distinction.
3. **Correlated exposure** — both sets concentrate in the Chicago-Naperville-Elgin corridor, where 22 of 29 top-50 tracts carry cluster-estimated growth. One regional growth assumption, mostly estimated.

**What-if weighting (FR-03).** Five numeric range parameters, one per component, 0–100 in steps of 5. The score measure normalises across whatever the sliders sum to:

```
Weighted Score =
VAR wHG = SELECTEDVALUE('w Household Growth'[Value])
VAR wMI = SELECTEDVALUE('w Median Income'[Value])
VAR wDG = SELECTEDVALUE('w Deposit Growth'[Value])
VAR wCS = SELECTEDVALUE('w Competitor Saturation'[Value])
VAR wUD = SELECTEDVALUE('w Unmet Demand'[Value])
VAR wTotal = wHG + wMI + wDG + wCS + wUD
RETURN
IF(
    wTotal = 0, BLANK(),
    DIVIDE(
        SUMX(index_components,
            index_components[household_growth_z] * wHG +
            index_components[median_income_z] * wMI +
            index_components[deposit_growth_z] * wDG +
            index_components[competitor_saturation_z] * wCS +
            index_components[unmet_demand_z] * wUD
        ),
        wTotal
    )
)
```

Sliders cannot be constrained to sum to 1, so normalise in the measure rather than asking the user to do arithmetic. A reset bookmark restores the shipped weights.

Place a small "top-50 LMI share" card beside the sliders. Moving `unmet_mortgage_demand` down visibly raises it — which demonstrates the sensitivity finding interactively and is the single most compelling thing on the dashboard.

---

## What does not ship

- No page of demographic charts. ACS data is an input, not a finding.
- No branch count by year chart on its own. The consolidation story belongs in the executive summary, where it has context.
- No composite "branch health score" blending level and trajectory. That distinction was made structural for a reason.
- No LMI map by itself. It invites reading geography as a target.
- No decorative KPI cards. Five on page 1 is the ceiling.

---

## Publishing

Per `05` §3: build in Desktop, publish interactive to Tableau Public, embed screenshots plus a 45–60 second recording in the case study, offer the `.pbix` as a download.

**The recording is the deliverable most recruiters will actually see.** Script it: eight seconds on page 1 showing the shortlist, ten on page 2 showing the scatter and one drillthrough, twenty on page 4 covering the comparison table and the 0-of-3 line, ten moving a weight slider and watching the LMI share move. Narrate the decision, not the technique.

## Build order

1. Load, set types, verify no leading-zero loss
2. Relationships, all single-direction
3. Measures — all of them, tested in a table visual before any chart exists
4. Page 4 first. It carries the finding, and if time runs short it is the page that must exist.
5. Pages 1, 2, then 3
6. Formatting pass: one font, one accent colour, consistent number formats
7. Screenshots and recording

---

## Implementation notes — added during build

These record where the generated project departs from the spec above, and why.
Nothing here overrides the spec; each item is either a correction to a factual
error or a deviation flagged for a decision.

### Publishing: Power BI does not publish to Tableau Public

The publishing section says "publish interactive to Tableau Public". Power BI
cannot — the two are different vendors' products. The equivalents are:

- **Publish to web** (Power BI Service, free tier) — gives a public embed URL,
  which is the closest match to what Tableau Public provides and is what the
  case study should link.
- **Power BI Service with a free account** — the report is viewable by the
  author but not anonymously, so it does not serve the recruiter case.
- **The `.pbix` download** — already planned, and works without any account.

Recommended: publish to web, embed the iframe plus screenshots, and keep the
`.pbix` (and the `.pbip` source) downloadable. Worth deciding before the case
study links anything.

### `fact_tract_lending` is not exported

The model table lists it at 474,707 rows. No visual in pages 1–4 reads it at
its own grain: page 3's capture rate and page 1's unmet-demand component are
already resolved by SQL-10 and SQL-11, which fix the definition of an
origination (`action_taken = '1'`, purchased loans excluded).

Shipping the grain would invite a reader to re-derive capture rate in DAX and
get a different number — purchased loans alone move it by 18.9%, unevenly. The
curated `tract_capture_rate` and `unmet_demand` outputs ship instead. Raise it
if a page genuinely needs the lending grain and it goes back in.

### Deviations that were adopted from the spec

- `index_components` is **wide** (`household_growth_z` … `unmet_demand_z`), as
  the spec's `Weighted Score` DAX assumes. An earlier long-form version worked
  but needed a `SUMX` over `VALUES` that no reader would check.
- `dim_year` gains a real `date` column and is marked as a date table, because
  `DATEADD` requires one. A year integer alone silently fails.
- `dim_tract` gains `in_catchment`, required by the `LMI Coverage` measure.
- Branch performance columns are merged onto `dim_branch`, as the spec's
  `AVERAGE(dim_branch[index_size_adjusted])` assumes.
- `recommended_sites` renamed `recommendation_sets`.

### What the generator cannot reliably author

Generated PBIR JSON covers pages, visuals, layout, and formatting. Three
things in the spec are fragile to hand-author and are better done once in
Desktop, where they take a few minutes each:

- **Bookmarks** (the Rule A / Rule B toggle, the weight reset)
- **Field parameters** (the page 2 index-basis toggle)
- **Drillthrough configuration and the back button** on page 3

The pages are generated with the visuals in place and the layout correct, so
these are additive rather than blocking.
