# Three Sites — Recommendation

Produced by `scripts/12_siting_recommendation.py` under the rule pre-registered
in [`siting_decision_rule.md`](siting_decision_rule.md), which was committed
before this computation ran. The git history is the evidence of that ordering.

## Current footprint

| | LMI households | non-LMI households |
|---|---|---|
| covered by any catchment | **42.49%** | **38.71%** |

Gap today: **+0.04 percentage points**.
4,776 tracts carry an LMI determination; **31 have no basis
by either route and are excluded from both numerator and denominator**, never
counted as non-LMI.

## The two sets

| measure | Rule A — commercial | Rule B — constrained |
|---|---|---|
| sites | 17093890402, 17093890105, 17163503404 | 17093890402, 17089850707, 55109120502 |
| summed opportunity score | 7.1603 | 6.4891 |
| delta LMI coverage | +0.0764% | +0.3742% |
| delta non-LMI coverage | +0.6418% | +0.3473% |
| binding test | FAIL | PASS |
| new tracts covered | 19 | 11 |
| new catchment households | 37,413 | 26,617 |
| unmet originations | 1,132 | 1,053 |
| distinct CBSAs | 2 | 2 |
| distinct counties | 2 | 3 |
| cluster-measured growth | 2 of 3 | 2 of 3 |
| LMI sites | 0 of 3 | 0 of 3 |

**Cost of the constraint: 0.6712 index points, 9.4% of the unconstrained score.**

The coverage figures above are computed twice by different means: here, from
projected distances during the search, and in `SQL-13` by set membership over
the emitted coverage sets. **They agree exactly**, which is the point of
emitting the sets rather than reimplementing the geometry in SQL.

That cross-check earned itself immediately. The first version of this script
emitted each rule's *new* catchments alone rather than the union with the
current footprint, and SQL-13 returned **negative** coverage deltas. Negative
is not merely implausible, it is impossible — a branch adds catchment area and
removes none. **The same monotonicity that makes AC-06 unfalsifiable is what
caught the bug**, because an impossible sign is a stronger signal than an
implausible magnitude.

The binding test is Δ LMI coverage ≥ Δ non-LMI coverage. Both deltas are
non-negative by construction, because a branch adds catchment area and removes
none. This tests whether expansion serves LMI households *proportionally*, not
whether it serves them at all — which is why it can fail where AC-06 cannot.

## Corridor concentration — the pre-registered prediction, tested

Amendment 1 predicted that corridor concentration and estimated-growth
dependence would prove to be the same tracts rather than two separate caveats,
because Census splits a tract when its population grows and growth corridors
are contiguous.

Across the top 50: the dominant CBSA holds **29 of 50**
tracts. Cluster-measured growth runs at
**22 of 29**
inside it against
**9 of 21**
outside. Correlation between the two, phi = **+0.336**.

The prediction holds: these are largely one exposure, not two.

## What a reviewer needs at the point of reading the shortlist

Per §5 of the rule, in the same place as the recommendation rather than an
appendix:

1. **LMI share of the shortlist** — the top 50 is 4.0% LMI against a 29.5%
   footprint baseline, a sevenfold under-representation with no income or race
   term anywhere in the ranking.
2. **Cluster-measured growth** — 62.0% of the top 50 carries a household growth
   value measured on an overlap cluster rather than on the tract itself,
   against 16.3% of all scored tracts.
3. **What the shortlist hangs on** — `unmet_mortgage_demand`, by top-50
   retention (28 of 50 survive its removal, the lowest of the five). That is
   the component deliberately left unadjusted in the scale test, and the one
   carrying the equity consequence.
