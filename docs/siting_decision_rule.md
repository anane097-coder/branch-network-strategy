# The Siting Decision Rule — pre-registered

**This document was written and committed BEFORE AC-06 was computed for any
recommended footprint.** That ordering is the point. The shortlist is 4.0% LMI
against a 29.5% footprint baseline, so the equity number is already known to be
poor; deciding what the recommendation does about it *after* seeing the
coverage figure would invite retrofitting a justification to whatever came out.
This project has been careful about that failure at every prior point and the
last place to abandon the habit is the one where the answer is uncomfortable.

Nothing below may be revised in the light of the results it produces. If the
rule turns out to be wrong, the revision goes in as a dated amendment with the
original text intact.

---

## 1. AC-06, as written, cannot fail

> **AC-06** — LMI coverage rate is computed for both current and recommended
> footprints.
> **UAT-08** — Both values present and explained.

That is a **reporting** criterion, not a guardrail. It passes when two numbers
exist. A recommendation that widened the LMI gap in every respect would satisfy
it in full, because the criterion tests presence rather than direction.

Worse, the obvious stronger reading — *coverage must not fall* — **also cannot
fail**, and for a structural reason: adding a branch adds catchment area and
never removes any, so LMI coverage after expansion is **monotonically greater
than or equal to** coverage before it. A criterion that no possible
recommendation can violate is not a check.

This is the third occurrence of the pattern the audit exists to catch: an
acceptance criterion that carries the authority of ground truth while testing
something other than what it appears to. AC-01 named the wrong population;
AC-03's tie-break guard could silently skip; AC-06 cannot fail.

**AC-06 is retained as written and reported as written** — it is a stated
deliverable and it will pass. It is not treated as evidence about equity. The
test that can fail is defined below and reported beside it.

## 2. The measures, defined before they are computed

Three distinct quantities have been used loosely as "LMI coverage" in this
project. They are not interchangeable and the constraint binds on exactly one.

| Measure | Definition | Role |
|---|---|---|
| **Over-index** | LMI share of *catchment* households − LMI share of *footprint* households | Currently **+1.9pp** (27.3% vs 25.4%). Descriptive. Reported, not binding. |
| **Coverage** | LMI households inside **any** subject catchment ÷ **all** LMI households in the footprint | The AC-06 measure. Monotone under expansion. Reported, not binding. |
| **Coverage delta gap** | Δ coverage for LMI households − Δ coverage for non-LMI households | **The binding test.** Can fail. Defined below. |

All three are computed on **households, not tracts** — the tract basis inflated
the over-index from +1.9pp to +3.3pp and the household basis is the one that
matches how an examiner reads a footprint.

LMI status uses `lmi_basis = 'ffiec_ratio'` where present and `'acs_fallback'`
where not, with the fallback labelled. The **31 tracts with no basis by either
route are excluded from both numerator and denominator** of every measure
above, not counted as non-LMI. They are enumerated in the quality log; 22 are
water or unpopulated and 9 are populated tracts named individually.

### The binding test

Let *C* be the current footprint and *R* the footprint after adding the
recommended sites.

```
Δ_LMI     = coverage_LMI(R)     − coverage_LMI(C)
Δ_nonLMI  = coverage_nonLMI(R)  − coverage_nonLMI(C)

PASS  when  Δ_LMI  ≥  Δ_nonLMI
```

In words: **expansion must extend service to LMI households at least as much
as it extends service to everyone else.** Both deltas are non-negative by
construction, so this is not a test of whether coverage improves — it is a test
of whether it improves *proportionally*. A recommendation can raise LMI
coverage in absolute terms while widening the gap, and that is precisely the
outcome the 4.0% shortlist predicts.

This test can fail. That is why it is the one that binds.

## 3. The selection rule

**Candidates.** Tracts with `opportunity_score` not null — 4,733 of 4,807. The
74 refused for missing components are not eligible; a site cannot be
recommended on a score that was never computed.

**Separation.** No two recommended sites may fall within the tier radius of one
another. Three tracts in adjacent Kendall County blocks are one market with
three pins in it, not three sites, and their catchments would largely coincide.
The radius is the one script 08 recomputes — no new parameter is introduced.

**Rule A — commercial.** The three highest-scoring candidates satisfying
separation. This is the unconstrained recommendation.

**Rule B — constrained.** The highest-scoring set of three candidates
satisfying separation **and** the binding test in §2. Searched over the top 200
candidates by score; if the constraint is satisfiable at all it will be
satisfiable well inside that range, and the bound is stated so the search is
reproducible rather than exhaustive-by-assumption.

**What ships.** **Rule B is the recommendation.** Rule A is reported beside it,
together with the **commercial cost of the constraint**, expressed as:

- difference in summed `opportunity_score`
- difference in catchment households reached
- difference in unmet mortgage originations within the recommended catchments

If Rule A already satisfies the binding test, the two coincide and the
constraint cost is zero — which is a result, not a formality.

**If no set of three satisfies the constraint,** that is reported as the
finding it is: *the opportunity model as constructed cannot produce a
three-site expansion that serves LMI households proportionally.* No fallback,
no relaxation of the test to make an answer appear. The next move in that case
is a stated choice between fewer sites, a different ranking basis, or an
explicit acknowledgement that commercial and CRA objectives diverge here — and
that choice belongs to the reader, with the numbers in front of them.

## 4. Why a constraint and not a re-weighting

The alternative was to add an equity term to the index, or to re-weight toward
`unmet_per_application`, until the shortlist looked acceptable. Both were
rejected for the same reason the ranking basis was not chosen on its equity
outcome in SQL-11: **a measure adjusted until it produces the desired answer no
longer provides evidence for that answer.**

A constraint keeps the two things separate and legible. The commercial model
says what it says; the constraint says what the bank will not do; the cost of
the constraint is the price of the policy and is stated in the open. That is
also how the decision is actually made in an institution where a CRA officer
holds a veto — which is the stakeholder structure `08` specifies. The officer
does not re-weight the analyst's model. They reject a shortlist.

## 5. What is reported regardless of outcome

Beside the recommended sites, at the point the shortlist is presented — not in
a limitations appendix:

1. **LMI share of the shortlist** against the 29.5% footprint baseline.
2. **The share of the shortlist whose household growth is cluster-measured
   rather than tract-measured.** In the top 50 this is 62.0% against 16.3% of
   all scored tracts. Tracts fragment because they grow, so the shortlist rests
   disproportionately on the weakest part of that component.
3. **Which component the shortlist hangs on** — `unmet_mortgage_demand`, by
   top-50 retention, which is the component deliberately left unadjusted and
   the one carrying the equity consequence.

These three belong in the same paragraph as the recommendation. A reviewer
reading the shortlist needs them at the moment they read it, not after.

---

*Pre-registered 2026-08-14, before AC-06 or any recommended footprint was
computed. Amendments below this line only, dated, original text unchanged.*

---

## Amendment 1 — 2026-08-14, still before any footprint was computed

### Corridor concentration: reported as a named risk, not constrained

Tier-radius separation guarantees that three sites do not serve overlapping
catchments. **It does not guarantee they represent three markets.** Kendall,
Kane and Will are three counties forming one contiguous exurban corridor west
of Chicago — geographically separated, economically correlated, and exposed to
a single regional growth assumption. Three pins in that corridor is one bet.

This is **reported, not constrained**, and the reasoning is deliberate:

- Three sites in one growth corridor may genuinely be the right commercial
  answer. A bank expanding into the Chicago collar counties on purpose is
  making a coherent strategic bet, not an error.
- A second hard constraint on top of proportionality risks **over-determining**
  a three-item selection drawn from a 50-item shortlist. Two binding
  constraints on three choices leaves almost nothing free, and the resulting
  set would be an artifact of the constraints rather than of the analysis.
- Correlated risk is a legitimate thing for a board to be told about. Telling
  them is the deliverable; deciding for them is not.

**What is reported, for whichever set is recommended:** the number of distinct
CBSAs and distinct counties among the three sites, and — where two or more
share a CBSA — an explicit statement that those sites share one growth
assumption.

### The two caveats are probably one caveat

Corridor concentration and estimated-growth dependence are being treated
throughout this project as separate limitations. **In this shortlist they are
likely the same tracts**, and the mechanism is not a coincidence:

1. Census splits a tract when its population grows.
2. A split tract's own 2019 household count is therefore apportioned rather
   than observed, so its growth is measured on an overlap cluster — 62.0% of
   the top 50 against 16.3% of all scored tracts.
3. High-growth tracts cluster geographically, because growth corridors are
   contiguous.

So the corridor and the cluster-measured tracts are the same population
arriving by two routes, and the shared exposure compounds rather than adds:
**a single regional growth assumption, measured for most of these tracts by
estimate rather than observation.**

**This is testable and will be tested**, not asserted: the overlap between
"sites sharing a CBSA" and "sites with `growth_is_estimated`" is reported for
the recommended set, and the correlation between the two is reported across
the whole top 50. If they turn out to be independent, that is a better result
than expected and will be stated as such.

*Amendment 1 pre-registered before computation, as above.*
