# Acceptance Criteria — Definitional Audit

AC-01 named a reference concept with two incompatible definitions, and the
wrong one would have reported catastrophic failure on a correct warehouse. A
criterion carries more authority than a query, because it reads as ground
truth. This audit puts every remaining criterion to the same question:

> **What population does this criterion's reference concept actually measure,
> and does it admit more than one definition?**

Each entry states the ambiguity, the definition adopted, and — where the
choice is consequential — what the alternative would have produced.

---

## AC-01 — deposits reconcile to FDIC · **RESOLVED**

**Ambiguity:** "FDIC published state totals" describes two different figures.
`/banks/summary` reports Call Report deposits for institutions *headquartered*
in a state; SOD allocates deposits to the *branch's* location.

**Adopted:** FDIC's server-side SOD aggregate, `ref_sod_state_totals`.

**The alternative:** would have reported Wisconsin **+50%** and Illinois
**+15%** off — a failure that is really a mismatch between two populations.

**On how this got written.** The ambiguity was latent from the moment the
criterion was drafted, not created by later work. `/banks/summary` is the
reading most people reach for first, both figures were known to exist, and the
shorter phrase was written anyway. That a warehouse was needed to *falsify* the
criterion does not mean it was well-specified before one existed. Both are
true: it needed a warehouse to catch, and it was underspecified when written.

That distinction is the whole argument for this audit. "A criterion written by
someone who knew both figures existed still encoded the wrong population" is
what justifies checking all seven, rather than treating AC-01 as an unlucky
one-off.

**Status:** PASS, exact, all 14 state-years.

---

## AC-02 — every branch resolves to exactly one tract

**Ambiguities, three of them:**

1. *Which branches?* All 6,467 observed across seven vintages, or the 5,263
   operating in the current one? **Adopted: all 6,467**, since historical
   deposits are analysed and a closed branch still needs a tract.
2. *Resolves how?* By coordinates, or by FDIC's reported county? These
   **disagree for 31 branches**. **Adopted: coordinates**, because in every
   case checkable by hand the branch's own address text agrees with the
   coordinates and not with `STCNTYBR`.
3. *Against which tract vintage?* TIGER 2024, matching the ACS 2020-boundary
   vintage. Mixing vintages silently produces wrong joins.

**Consequential:** yes. Under definition (2)'s alternative, 31 branches would
sit in different markets.

**Status:** PASS with exceptions enumerated — 1 branch has no coordinates; 31
county disagreements listed in `docs/spatial_join_exceptions.md`.
**AC-02 does not detect (2)** — all 31 resolve to exactly one tract and pass
cleanly. The criterion is weaker than it reads.

---

## AC-03 — every tract in at most one primary catchment

**Ambiguities:**

1. *Which tracts?* Only the 1,849 covered. The 2,958 uncovered have zero
   primaries, which satisfies "at most one" trivially — the criterion is
   silent on coverage and must not be read as implying it.
2. *What makes a catchment primary?* The tie-break. **Adopted:
   `nearest_branch`.**

**Consequential: severely.** Under a largest-deposit tie-break, **59.8% of
contested tracts change hands** and the median branch's catchment household
base moves **31.8%**. AC-03 passes identically under both, so **the criterion
cannot distinguish them.**

**Why the adopted definition is not merely a preference:** largest-deposit is
*circular* for BQ-3 — assigning tracts by deposit size and then predicting
deposits from catchment composition makes the index measure its own input.
`nearest_branch` uses only geography, no outcome variable.

**Status:** PASS, tested twice (script 08 and in-warehouse).

---

## AC-04 — clean re-run from raw produces identical index values

**Ambiguities:**

1. *Does "from raw" include re-downloading?* It must not. The FDIC and FFIEC
   APIs are live and their contents change; a true re-fetch would legitimately
   produce different data. **Adopted: from the pinned files in `data/raw/`,
   whose SHA-256 values are recorded in the manifest.** Reproducibility is
   asserted against fixed inputs, not against the internet.
2. *Identical to what precision?* Floating-point aggregation order can differ.
   **Adopted: exact equality on ranks and on values rounded to 6 decimal
   places**, which is far tighter than any interpretation of the results.

**Status:** not yet testable — the index does not exist (script 10). The
warehouse is dropped and rebuilt on every run, so no state survives between
runs to mask a failure.

---

## AC-05 — setting any weight to zero changes the ranking

**Ambiguities:**

1. *Which ranking?* All 4,807 tracts, or the top 10 that the recommendation
   actually uses? **Adopted: the top 25 by index**, reported alongside the
   full-set change. A component can be genuinely wired in and still not move
   the top 10, and the criterion's purpose is to prove wiring.
2. *How much change counts?* **Adopted: any change in ordinal position**, and
   the magnitude reported rather than reduced to pass/fail.

**Consequential:** moderate. Under a top-10-only reading, a real component
could fail a test it should pass.

**Status:** not yet testable (script 10/11).

---

## AC-06 — LMI coverage computed for current and recommended footprints

**This is the criterion most exposed to the AC-01 failure mode**, and it sits
closest to fair-lending territory where a wrong number does real damage.

**Ambiguities, four:**

1. *Coverage of what — tracts or households?* **Already measured both, and
   they disagree materially.** Tract basis: 33.2% inside vs 29.9% overall,
   **+3.3pp**. Household basis: 27.3% vs 25.4%, **+1.9pp**. LMI tracts are
   five times denser and five times smaller than non-LMI tracts, so counting
   tracts over-weights them. **Adopted: households.** The tract figure may
   appear only labelled as the naive comparison.
2. *Coverage by any catchment, or by primary catchment?* **Adopted: any**,
   since a branch serves customers regardless of which branch is nearest.
   Primary-only would understate coverage and would inherit the tie-break
   sensitivity documented under AC-03.
3. *Which LMI basis?* The FFIEC published ratio covers 4,772 tracts; an ACS
   reconstruction covers 4 more; 31 have neither. **Adopted: FFIEC primary,
   ACS fallback labelled `acs_fallback`** so a reconstruction is never
   silently mixed with the official figure examiners use.
4. *What happens to the 31 tracts with no basis?* **Adopted: excluded from
   both numerator and denominator, and reported as a named exception list**
   — 22 are water or special land use and 9 are populated. Collapsing them
   into "not LMI" would inflate the denominator and improve the equity result
   in the favourable direction, which is the `lmi_flag` NA→False error again.

**Status:** current footprint measured (+1.9pp, household basis). Recommended
footprint awaits script 10.

---

## BQ-3 performance index — not an acceptance criterion, but the same risk

"Actual deposits ÷ deposits predicted from catchment potential" contains three
undefined terms:

1. *Catchment potential over which tracts?* Primary only, or all tracts within
   radius? **This is the tie-break sensitivity again** — the two differ by a
   median 31.8% per branch. **Adopted: primary**, so each tract's households
   are counted once across the network rather than double-counted into several
   branches.
2. *Potential measured how?* Households × median income, per the KPI table.
   **Suppressed-income tracts (84) must not contribute zero** — that would
   understate potential and make the branch look like an over-performer.
   **Adopted: excluded from the sum, with the excluded count reported per
   branch**, so a branch whose catchment is thinly measured is visible.
3. *Actual deposits at which vintage?* **Adopted: the current vintage**, with
   `position_drift_miles` flagged for the 2 branches that genuinely relocated
   more than 25% of their tier radius — their deposits were earned partly at a
   different location.
4. *Are headquarters branches in the index at all?* **Decided before the query
   exists, deliberately.** SOD books deposits where the account was opened, so
   a head office carries corporate and brokered balances with **no
   relationship to its catchment's demographics**. Measured: Brown County
   (Green Bay, Associated's head office) holds **18.6% of the county's
   branches and 51.7% of its deposits — a +33pp gap**. Left in, such a branch
   produces the most extreme "over-performance" in the index by construction,
   and it would sit at the top of the distribution looking like the network's
   best site. **Adopted: main offices (`is_main_office = '1'`) are excluded
   from the performance index and reported separately**, not silently dropped.
   The index measures how well a branch converts local demand; a head office
   is not doing that job and should not be scored as though it were.

   This also protects the recommendation. BQ-5 sites new branches partly on
   what the index says works — and "put a branch where the head office is"
   is the conclusion an unadjusted index would support.

### The same circularity, twice, in unrelated parts of the model

The headquarters exclusion and the `nearest_branch` tie-break are the same
error in different clothing: **an outcome variable contaminating the input to
a question about outcomes.**

| Where | The contamination | If left in |
|---|---|---|
| Catchment tie-break | Assign tracts by *deposit size*, then predict deposits from catchment composition | The index measures its own input |
| Performance index membership | Score a *head office* — whose deposits are booked, not earned locally — against local demographics | The index rewards booking, and BQ-5 then sites branches accordingly |

Neither was found by a test designed to catch circularity. Both were found by
asking what a figure is made of before using it to judge something else. The
rule that generalises: **before a variable enters a model as an input, check
that it is not downstream of the thing the model is supposed to explain.**

---

## Summary

| Criterion | Ambiguity found | Consequential | Resolved |
|---|---|---|---|
| AC-01 | Two published figures differing 50% | **Yes** | Yes |
| AC-02 | Branch set, resolution method, tract vintage | Yes — 31 branches | Yes |
| AC-03 | Tie-break definition | **Yes — 59.8% of contested tracts** | Yes |
| AC-04 | Re-fetch vs pinned raw; float precision | Yes | Defined, untested |
| AC-05 | Which ranking; what counts as change | Moderate | Defined, untested |
| AC-06 | Tracts vs households; basis; exception handling | **Yes — +3.3pp vs +1.9pp** | Yes |
| BQ-3 | Catchment scope; suppressed tracts; vintage | **Yes** | Defined |

Every criterion examined admitted at least one alternative reading, and four
of seven were consequential enough to change a reported result.

---

## Open decisions for SQL-12 and script 11 — settle before, not after

**1. Every index component is scale-exposed, four of five as ratios or rates.**
The performance index's scale bias inverted a market ranking; the same test
must run on all five opportunity-score components before they are combined,
because a composite inherits every component's scale relationship at once.

| Component | Exposure |
|---|---|
| `competitor_saturation` | Branches ÷ households — the same shape that failed |
| `unmet_mortgage_demand` | Scales with tract size unless normalised |
| `deposit_market_growth` | County CAGR; small counties volatile by construction |
| `household_growth` | Rate over a small denominator: 200→240 households reads +20%, 2,000→2,300 reads +15%, and the first outranks the second on an opportunity an order of magnitude smaller |
| `median_income` | Not a ratio, but a **derived level** whose ACS margin of error scales inversely with tract sample size. Precision varies systematically across the thing being ranked, so small-sample tracts populate both tails more readily than their true values warrant — which inflates their weight specifically under **min-max** normalisation, the method `config/index_weights.yaml` currently sets |

**2. The adjustment method is itself a free choice, and this model has already
been bitten twice by those.** Size-banding must not be traded for a new
artifact: if the relationship is smooth, band boundaries create discontinuities
where two nearly identical tracts land either side and receive different
adjustments. **Look at the scatter first.** If the relationship is continuous,
a regression-based adjustment has no boundary to defend and is cleaner; if
banding is used, the bands must be derived by a stated rule — as the catchment
radii were — rather than drawn to look reasonable.

**3. Script 11's sensitivity must vary weights across the ADJUSTED components,
not the raw ones.** Otherwise it reports the robustness of a model that is not
the one shipping. Obvious written down, and exactly the kind of thing that goes
wrong when two corrections land in different scripts. Noted here because this
document is read before both.

**4. The `unmet_mortgage_demand` component test and the SQL-11 basis decision
must not cross.** SQL-11 emits four ranking bases and adopts none; whichever it
feeds forward is the series the component scale test will actually be testing.
So the order is fixed: **settle the basis, then scale-test the series that
ships.** If the basis changes after the test has run, the test is void and must
be rerun — a scale result computed on `basis_absolute` says nothing about
`basis_per_application`, which is size- and affluence-neutral by construction
and may well need no adjustment at all.

This is the same failure shape as item 3: two corrections landing in different
artifacts with neither aware of the other. Item 3 is a script-to-script version;
this is a query-to-script version. Both are recorded here because this document
is read before either.

---

## The equity result has now been protected three times, at three levels

This deserves separating from the general wrong-population pattern, because
all three instances share a direction as well as a shape.

| Level | The default that was nearly taken | Effect on BQ-6 |
|---|---|---|
| **Value** | `lmi_flag` NA collapsed to `False` | Inflates the non-LMI count — bank looks better |
| **Measure** | LMI coverage on tract counts rather than households | Overstates over-indexing, +3.3pp vs the true +1.9pp |
| **Criterion** | The 31 no-basis tracts folded into "not LMI" | Inflates the denominator — bank looks better |

**Every one of the three would have made the subject look better on
fair-lending exposure.** None raised an error. Each was caught by asking what
population the figure covered, not by a test designed to catch it.

That direction is the point. An analyst interrogates a result that looks bad
and accepts one that looks good, so **errors flattering the subject are the
least likely to be examined** — which is exactly why they need a standing rule
rather than case-by-case vigilance. The rule, recorded in the working
instructions: unknown is not a value, and a default that moves a headline
metric favourably is a defect until proven otherwise.

In a project whose subject is a real named bank and whose adjacent territory
is fair lending, that habit is not a nicety. `05` §4 is explicit that a
market-opportunity score deprioritising lower-income tracts is a redlining
model regardless of intent. A pipeline that rounds three separate unknowns in
the bank's favour would produce exactly that, with every individual step
looking defensible.
