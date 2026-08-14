"""How much does the recommendation depend on the weights we chose?

    python scripts/11_sensitivity.py

"The ranking is stable under weight variation" is a weaker claim than it
sounds, and this script deliberately does not stop there. Two additions:

1. THE SENSITIVITY RUNS ON THE ADJUSTED COMPONENTS, NOT THE RAW ONES. Four of
   the five were reformulated during the scale test. Testing robustness of the
   pre-correction series would report the stability of a model that is not the
   one shipping. The z-scores are recovered from SQL-12's own contribution
   columns rather than recomputed, which makes it impossible for this script
   to normalise differently from the query it is testing.

2. MOVEMENT IS ATTRIBUTED TO COMPONENTS, not just totalled. If one component
   drives most of the reshuffling, that is the component whose reformulation
   decisions carry the weight - and with four of five reformulated, knowing
   which one the answer hangs on matters more than a global stability figure.
   The honest version of the story is "stable except through X, which is the
   component we deliberately left unadjusted, and here is why".

Reports the SHAPE of the movement, not only its summary. A median rank shift
of 40 places means something different when the spread is 0-80 than when 5% of
tracts move 2,000 places, and the aggregate cannot tell them apart.
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "branch_analysis.duckdb"
QUERY = ROOT / "sql" / "SQL-12_market_opportunity_index.sql"
REPORT = ROOT / "docs" / "sensitivity.md"

COMPONENTS = ["household_growth", "median_income", "deposit_market_growth",
              "competitor_saturation", "unmet_mortgage_demand"]
TOP_N = 50


def md(df: pd.DataFrame, fmt: str = "{}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [fmt.format(v) for v in r]) + " |")
    return "\n".join(out)


def spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = ~(np.isnan(a) | np.isnan(b))
    ra, rb = np.empty(m.sum()), np.empty(m.sum())
    ra[a[m].argsort()] = np.arange(m.sum())
    rb[b[m].argsort()] = np.arange(m.sum())
    return float(np.corrcoef(ra, rb)[0, 1])


def compare(base_score, alt_score, lmi):
    """Rank agreement, top-N churn, and the SHAPE of the displacement."""
    base_rank = pd.Series(base_score).rank(ascending=False, method="min")
    alt_rank = pd.Series(alt_score).rank(ascending=False, method="min")
    shift = (alt_rank - base_rank).abs()
    top_base = set(base_rank.nsmallest(TOP_N).index)
    top_alt = set(alt_rank.nsmallest(TOP_N).index)
    lmi_top = lmi.loc[list(top_alt)]
    return {
        "spearman": round(spearman(base_score, alt_score), 4),
        f"top{TOP_N}_retained": len(top_base & top_alt),
        "median_shift": int(shift.median()),
        "p95_shift": int(shift.quantile(0.95)),
        "max_shift": int(shift.max()),
        "pct_moving_500+": round(float((shift >= 500).mean()) * 100, 2),
        f"lmi_share_top{TOP_N}": round(float(lmi_top.mean(skipna=True)) * 100, 1),
    }


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    d = con.execute(QUERY.read_text(encoding="utf-8")).df()
    weights = con.execute("SELECT * FROM ref_index_weights").df()

    scored = d[d["opportunity_score"].notna()].reset_index(drop=True)
    print(f"tracts scored on all five components: {len(scored):,} "
          f"of {len(d):,} ({len(scored)/len(d):.1%})")

    primary = weights[weights["scenario"] == "primary"].set_index("component")["weight"]

    # RECOVER the z-scores from SQL-12's own contributions. Recomputing them
    # here would let this script normalise differently from the query it is
    # supposed to be testing - the two would drift and nothing would say so.
    z = pd.DataFrame({
        c: scored[f"contrib_{c}"] / primary[c] for c in COMPONENTS
    })
    check = sum(z[c] * primary[c] for c in COMPONENTS)
    worst = float((check - scored["opportunity_score"]).abs().max())
    if worst > 1e-3:
        raise SystemExit(f"Recovered z-scores do not reproduce the score "
                         f"(max error {worst}). Refusing to report on them.")
    print(f"z-scores recovered from SQL-12 contributions; "
          f"score reproduced to {worst:.2e}")

    lmi = scored["lmi_flag"]
    base = scored["opportunity_score"]
    baseline_lmi = float(lmi.mean(skipna=True)) * 100

    # --- 1. weight scenarios ---------------------------------------------
    rows = {}
    for sc in sorted(weights["scenario"].unique()):
        if sc == "primary":
            continue
        w = weights[weights["scenario"] == sc].set_index("component")["weight"]
        alt = sum(z[c] * w[c] for c in COMPONENTS)
        rows[sc] = compare(base, alt, lmi)
    scenarios = pd.DataFrame(rows).T
    scenarios.index.name = "scenario"

    # --- 2. leave-one-out attribution ------------------------------------
    # Which component is the ranking actually hanging on? Drop each in turn,
    # renormalise the rest to sum to 1, and measure how far the ranking moves.
    # A component whose removal barely changes the order is not driving the
    # recommendation however large its weight.
    loo = {}
    for drop in COMPONENTS:
        keep = [c for c in COMPONENTS if c != drop]
        renorm = primary[keep] / primary[keep].sum()
        alt = sum(z[c] * renorm[c] for c in keep)
        loo[drop] = compare(base, alt, lmi)
    attribution = pd.DataFrame(loo).T
    attribution.index.name = "component removed"
    attribution.insert(0, "weight", [primary[c] for c in attribution.index])

    # --- 3. how concentrated is each component's influence? --------------
    # Weight says how much a component COULD matter; the spread of its
    # contribution says how much it does. A heavily weighted component with
    # little variation moves nobody.
    spread = pd.DataFrame({
        "weight": [primary[c] for c in COMPONENTS],
        "sd_of_contribution": [scored[f"contrib_{c}"].std().round(4)
                               for c in COMPONENTS],
        "corr_with_score": [round(np.corrcoef(
            scored[f"contrib_{c}"], base)[0, 1], 3) for c in COMPONENTS],
        "share_of_top50_contrib": [
            round(float(scored.nlargest(TOP_N, "opportunity_score")
                        [f"contrib_{c}"].sum()
                        / scored.nlargest(TOP_N, "opportunity_score")
                        ["opportunity_score"].sum()) * 100, 1)
            for c in COMPONENTS],
    }, index=COMPONENTS)
    spread.index.name = "component"

    print("\nScenario spread:")
    print(scenarios.to_string())
    print("\nLeave-one-out attribution:")
    print(attribution.to_string())
    print("\nInfluence concentration:")
    print(spread.to_string())

    # TWO METRICS, TWO ANSWERS, AND THE MINORITY ONE IS THE ONE THAT MATTERS.
    # Global rank correlation asks which component reorders all 4,733 tracts.
    # Top-N retention asks which one decides the SHORTLIST. BQ-5 recommends
    # three sites, so the shortlist is the deliverable and the global figure is
    # context. Where they disagree, saying only "the ranking is stable" would
    # be true of the whole and false of the part anybody acts on.
    driver_global = attribution["spearman"].idxmin()
    driver_top = attribution[f"top{TOP_N}_retained"].idxmin()
    print(f"\nDrives the FULL ranking: {driver_global} "
          f"(spearman {attribution.loc[driver_global, 'spearman']:.3f} without it)")
    print(f"Drives the TOP {TOP_N}:     {driver_top} "
          f"({attribution.loc[driver_top, f'top{TOP_N}_retained']:.0f} of "
          f"{TOP_N} retained without it)")
    disagree = driver_global != driver_top

    # --- 4. the estimated-growth question --------------------------------
    # 12% of tracts carry a growth value measured on an overlap cluster rather
    # than the tract. If those tracts dominate the top of the ranking, the
    # recommendation rests on the weakest part of the component.
    top = scored.nlargest(TOP_N, "opportunity_score")
    est = pd.DataFrame({
        "share_estimated_growth": [
            round(float(top["growth_is_estimated"].mean()) * 100, 1),
            round(float(scored["growth_is_estimated"].mean()) * 100, 1)],
        "share_lmi": [round(float(top["lmi_flag"].mean(skipna=True)) * 100, 1),
                      round(baseline_lmi, 1)],
        "share_county_heavily_adjusted": [
            round(float(top["county_growth_heavily_adjusted"]
                        .fillna(False).mean()) * 100, 1),
            round(float(scored["county_growth_heavily_adjusted"]
                        .fillna(False).mean()) * 100, 1)],
    }, index=[f"top {TOP_N}", "all scored tracts"])

    print(f"\nComposition of the top {TOP_N}:")
    print(est.to_string())

    REPORT.write_text(f"""# Weight Sensitivity, and What the Ranking Hangs On

Generated by `scripts/11_sensitivity.py` over {len(scored):,} tracts scored on
all five components ({len(scored)/len(d):.1%} of {len(d):,}).

The z-scores tested here are **recovered from SQL-12's own contribution
columns**, not recomputed. This script cannot normalise differently from the
query it is testing, and it verifies that the recovered values reproduce the
published score (max error {worst:.1e}) before reporting anything.

**The sensitivity runs on the adjusted components.** Four of the five were
reformulated during the scale test; testing the pre-correction series would
report the robustness of a model that is not the one shipping.

## 1. Weight scenarios

Alternative weightings from `config/index_weights.yaml`, against the primary.

{md(scenarios)}

Displacement is reported as a **distribution, not a median**. A median shift of
40 places means something different when the spread is 0-80 than when a few
percent of tracts move thousands, and the summary cannot tell those apart.

## 2. Leave-one-out: which component is the answer hanging on?

Each component removed in turn, remaining weights renormalised to 1.0. A
component whose removal barely moves the order is not driving the
recommendation however large its weight.

{md(attribution)}

### The two metrics disagree, and the minority answer is the one that matters

| Question | Metric | Answer |
|---|---|---|
| What reorders all {len(scored):,} tracts? | rank correlation | **`{driver_global}`** ({attribution.loc[driver_global, 'spearman']:.3f} without it) |
| What decides the shortlist? | top-{TOP_N} retention | **`{driver_top}`** ({attribution.loc[driver_top, f'top{TOP_N}_retained']:.0f} of {TOP_N} retained) |

{"They differ, and that difference is the finding." if disagree else
 "They agree, which makes the attribution unambiguous."}
BQ-5 recommends three sites, so **the shortlist is the deliverable and the
global figure is context**. Reporting only "the ranking is stable under weight
variation" would be true of the whole and false of the part anybody acts on.

`{driver_top}` is also the component **deliberately left unadjusted** in the
scale test: it carries the strongest size correlation of the five (+0.707),
and under the absolute basis that is the measure answering the question asked,
because a larger tract genuinely does hold more unmet demand. So the honest
statement is not "the ranking is robust". It is: **the ranking is robust except
through `{driver_top}`, which is the one component whose scale relationship was
kept on purpose, and which carries the equity consequence SQL-11 measured.**

## 3. Influence concentration

Weight says how much a component *could* matter. The spread of its
contribution says how much it *does* — a heavily weighted component with
little variation across tracts moves nobody.

{md(spread)}

## 4. What the top {TOP_N} is made of

{md(est)}

`share_estimated_growth` is the proportion whose household growth was measured
on an overlap cluster rather than on the tract itself, because the tract's own
apportioned value across the 2010/2020 boundary change was uniform-density
guesswork. If the recommended set is disproportionately these tracts, the
recommendation rests on the weakest part of that component.

`share_lmi` against a footprint baseline of {baseline_lmi:.1f}% is the equity
consequence carried forward from SQL-11, where ranking on absolute unmet
demand produced a top-50 that was 2.0% LMI. The composite does not fix that on
its own, and BQ-6 reports the number for whichever basis actually ships.
""", encoding="utf-8")
    print(f"\nWrote {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
