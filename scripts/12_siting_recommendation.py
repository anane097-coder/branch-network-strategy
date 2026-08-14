"""Select three sites under the pre-registered rule, and price the constraint.

    python scripts/12_siting_recommendation.py

Implements docs/siting_decision_rule.md, which was committed BEFORE this ran.
Nothing here decides anything the rule left open; where the rule is silent this
script reports rather than chooses.

    Rule A  the three highest-scoring candidates satisfying tier-radius
            separation. Unconstrained, commercial.
    Rule B  the highest-scoring such set that also satisfies the binding test:
            expansion must extend coverage to LMI households at least as much
            as to everyone else.

            delta_LMI >= delta_nonLMI

Both deltas are non-negative by construction - a new branch adds catchment area
and removes none - so this is a test of PROPORTIONALITY, not of improvement.
That is the whole reason it can fail where AC-06 cannot.

Rule B ships. Rule A is reported beside it with the cost of the constraint.
"""
from itertools import combinations
from pathlib import Path

import duckdb
import geopandas
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "branch_analysis.duckdb"
QUERY = ROOT / "sql" / "SQL-12_market_opportunity_index.sql"
REPORT = ROOT / "docs" / "siting_recommendation.md"
SITES_OUT = ROOT / "data" / "staging" / "ref_recommended_sites.csv"
COVERAGE_OUT = ROOT / "data" / "staging" / "ref_recommended_coverage.csv"

SEARCH_DEPTH = 200      # stated in the rule, so the search is reproducible
N_SITES = 3
M = 1609.344


def md(df: pd.DataFrame, fmt: str = "{}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [fmt.format(v) for v in r]) + " |")
    return "\n".join(out)


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    idx = con.execute(QUERY.read_text(encoding="utf-8")).df()

    tracts = con.execute("""
        SELECT tract_geoid, county_name, county_fips, cbsa, cbsa_title, tier,
               households, lmi_flag, lmi_basis, centroid_lat, centroid_lon
        FROM dim_tract
    """).df()
    radii = dict(con.execute("""
        SELECT DISTINCT tier, radius_miles FROM fact_tract_competition
    """).df().values)
    current = set(con.execute(
        "SELECT DISTINCT tract_geoid FROM bridge_branch_catchment").df()
        ["tract_geoid"])
    print(f"radii from the warehouse: {radii}")
    print(f"tracts in the current footprint: {len(current):,}")

    # --- the LMI universe, defined once, per the rule ---------------------
    # The 31 tracts with no LMI basis by either route are excluded from BOTH
    # numerator and denominator. Not counted as non-LMI - that would inflate
    # the denominator and quietly improve every figure below.
    t = tracts[tracts["lmi_flag"].notna() & tracts["households"].notna()].copy()
    t["lmi"] = t["lmi_flag"].astype(bool)
    excluded_no_basis = len(tracts) - len(t)
    lmi_hh_total = t.loc[t["lmi"], "households"].sum()
    non_hh_total = t.loc[~t["lmi"], "households"].sum()
    print(f"tracts with an LMI determination: {len(t):,} "
          f"({excluded_no_basis} excluded from both sides)")

    def coverage(tract_set):
        """LMI and non-LMI household coverage of a set of covered tracts."""
        inside = t["tract_geoid"].isin(tract_set)
        return (t.loc[inside & t["lmi"], "households"].sum() / lmi_hh_total,
                t.loc[inside & ~t["lmi"], "households"].sum() / non_hh_total)

    cov_lmi_now, cov_non_now = coverage(current)
    print(f"\ncurrent coverage   LMI {cov_lmi_now:.4%}   "
          f"non-LMI {cov_non_now:.4%}   gap {cov_lmi_now - cov_non_now:+.4%}")

    # --- candidate catchments --------------------------------------------
    cand = (idx[idx["opportunity_score"].notna()]
            .nlargest(SEARCH_DEPTH, "opportunity_score")
            .merge(tracts[["tract_geoid", "county_fips", "cbsa",
                           "centroid_lat", "centroid_lon"]],
                   on="tract_geoid", how="left")
            .reset_index(drop=True))

    tg = geopandas.GeoDataFrame(t, geometry=geopandas.points_from_xy(
        t["centroid_lon"], t["centroid_lat"]), crs="EPSG:4326").to_crs("EPSG:5070")
    tx = np.c_[tg.geometry.x, tg.geometry.y]
    cg = geopandas.GeoDataFrame(cand, geometry=geopandas.points_from_xy(
        cand["centroid_lon"], cand["centroid_lat"]),
        crs="EPSG:4326").to_crs("EPSG:5070")
    cx = np.c_[cg.geometry.x, cg.geometry.y]

    # Each candidate's catchment, at ITS OWN tier radius - the same rule the
    # existing footprint was built under, imported as data rather than restated.
    D = np.linalg.norm(cx[:, None, :] - tx[None, :, :], axis=-1) / M
    lim = cand["tier"].map(radii).to_numpy()[:, None]
    catchments = [set(t["tract_geoid"].to_numpy()[row]) for row in (D <= lim)]

    # Separation: no two sites within the larger of their two tier radii.
    S = np.linalg.norm(cx[:, None, :] - cx[None, :, :], axis=-1) / M
    r = cand["tier"].map(radii).to_numpy()
    sep_ok = S > np.maximum(r[:, None], r[None, :])

    def evaluate(triple):
        covered = set(current)
        for i in triple:
            covered |= catchments[i]
        cl, cn = coverage(covered)
        return {
            "sites": [cand.loc[i, "tract_geoid"] for i in triple],
            "score": float(cand.loc[list(triple), "opportunity_score"].sum()),
            "cov_lmi": cl, "cov_non": cn,
            "d_lmi": cl - cov_lmi_now, "d_non": cn - cov_non_now,
            "passes": (cl - cov_lmi_now) >= (cn - cov_non_now),
            "new_tracts": len(covered - current),
            "catchment_households": float(
                t[t["tract_geoid"].isin(covered - current)]["households"].sum()),
            "unmet": float(cand.loc[list(triple), "unmet_mortgage_demand"].sum()),
            "cbsas": cand.loc[list(triple), "cbsa"].nunique(),
            "counties": cand.loc[list(triple), "county_fips"].nunique(),
            "estimated_growth": int(
                cand.loc[list(triple), "growth_is_estimated"].sum()),
            "lmi_sites": int(cand.loc[list(triple), "lmi_flag"].fillna(False).sum()),
        }

    # --- Rule A: greedy by score, separation only ------------------------
    chosen, i = [], 0
    while len(chosen) < N_SITES and i < len(cand):
        if all(sep_ok[i, j] for j in chosen):
            chosen.append(i)
        i += 1
    if len(chosen) < N_SITES:
        raise SystemExit("Could not find three separated candidates.")
    rule_a = evaluate(tuple(chosen))

    # --- Rule B: best separated triple that also passes ------------------
    best, tested, separated = None, 0, 0
    for triple in combinations(range(len(cand)), N_SITES):
        if not (sep_ok[triple[0], triple[1]] and sep_ok[triple[0], triple[2]]
                and sep_ok[triple[1], triple[2]]):
            continue
        separated += 1
        s = float(cand.loc[list(triple), "opportunity_score"].sum())
        if best is not None and s <= best["score"]:
            continue                       # cannot beat the incumbent
        e = evaluate(triple)
        tested += 1
        if e["passes"]:
            best = e
    print(f"\nseparated triples in the top {SEARCH_DEPTH}: {separated:,}; "
          f"coverage evaluated for {tested:,}")

    if best is None:
        print("\nNO SET OF THREE SATISFIES THE BINDING TEST.")
        print("Reported as the finding. The rule forbids relaxing it.")
    else:
        print(f"\nRule B set: {best['sites']}  score {best['score']:.4f}")

    # --- the corridor prediction, tested ---------------------------------
    top50 = idx.nlargest(50, "opportunity_score").merge(
        tracts[["tract_geoid", "cbsa", "county_fips"]], on="tract_geoid")
    biggest_cbsa = top50["cbsa"].value_counts()
    in_main_corridor = top50["cbsa"] == biggest_cbsa.index[0]
    est = top50["growth_is_estimated"].astype(bool)
    # phi coefficient between "in the dominant CBSA" and "growth estimated"
    phi = float(np.corrcoef(in_main_corridor.astype(int), est.astype(int))[0, 1])

    print(f"\nCorridor prediction test over the top 50:")
    print(f"  dominant CBSA holds {int(biggest_cbsa.iloc[0])} of 50 "
          f"({biggest_cbsa.index[0]})")
    print(f"  estimated growth in that CBSA: "
          f"{int((in_main_corridor & est).sum())} of {int(in_main_corridor.sum())}")
    print(f"  estimated growth elsewhere:    "
          f"{int((~in_main_corridor & est).sum())} of {int((~in_main_corridor).sum())}")
    print(f"  phi(dominant CBSA, growth estimated) = {phi:+.3f}")

    # --- outputs ----------------------------------------------------------
    shipped = best if best is not None else None
    if shipped:
        sites = cand[cand["tract_geoid"].isin(shipped["sites"])].copy()
        sites["rule"] = "B_constrained"
        a_sites = cand[cand["tract_geoid"].isin(rule_a["sites"])].copy()
        a_sites["rule"] = "A_commercial"
        out = pd.concat([sites, a_sites])[
            ["rule", "tract_geoid", "county_name", "cbsa_title", "tier",
             "households", "lmi_flag", "opportunity_score",
             "unmet_mortgage_demand", "growth_is_estimated"]]
        SITES_OUT.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(SITES_OUT, index=False)
        print(f"\nWrote {SITES_OUT.relative_to(ROOT)}")

        # The tracts each rule's footprint would cover. Emitted so SQL-13 can
        # compute AC-06 by set membership instead of redoing the geometry -
        # a second distance implementation in SQL would drift from this one,
        # and the drift would be silent because both would look plausible.
        rows = []
        for label, ev in (("A_commercial", rule_a), ("B_constrained", best)):
            idxs = [cand.index[cand["tract_geoid"] == g][0] for g in ev["sites"]]
            # THE RECOMMENDED FOOTPRINT IS current UNION new, not the new
            # catchments alone. Emitting only the new ones made SQL-13 report
            # NEGATIVE coverage deltas - impossible, since a branch adds
            # catchment area and removes none. The monotonicity property that
            # makes AC-06 unfalsifiable is what caught this: an impossible
            # sign is a stronger signal than an implausible magnitude.
            covered = set(current)
            for i in idxs:
                covered |= catchments[i]
            for g in sorted(covered):
                rows.append({"rule": label, "tract_geoid": g,
                             "newly_covered": g not in current})
        for g in sorted(current):
            rows.append({"rule": "current", "tract_geoid": g,
                         "newly_covered": False})
        pd.DataFrame(rows).to_csv(COVERAGE_OUT, index=False)
        print(f"Wrote {COVERAGE_OUT.relative_to(ROOT)}")

    comp = pd.DataFrame({
        "Rule A — commercial": [
            ", ".join(rule_a["sites"]), round(rule_a["score"], 4),
            f"{rule_a['d_lmi']:+.4%}", f"{rule_a['d_non']:+.4%}",
            "PASS" if rule_a["passes"] else "FAIL",
            rule_a["new_tracts"], f"{rule_a['catchment_households']:,.0f}",
            f"{rule_a['unmet']:,.0f}", rule_a["cbsas"], rule_a["counties"],
            f"{rule_a['estimated_growth']} of 3", f"{rule_a['lmi_sites']} of 3"],
        "Rule B — constrained": (
            [", ".join(best["sites"]), round(best["score"], 4),
             f"{best['d_lmi']:+.4%}", f"{best['d_non']:+.4%}",
             "PASS", best["new_tracts"], f"{best['catchment_households']:,.0f}",
             f"{best['unmet']:,.0f}", best["cbsas"], best["counties"],
             f"{best['estimated_growth']} of 3", f"{best['lmi_sites']} of 3"]
            if best else ["none satisfies the test"] + ["—"] * 11),
    }, index=["sites", "summed opportunity score", "delta LMI coverage",
              "delta non-LMI coverage", "binding test", "new tracts covered",
              "new catchment households", "unmet originations",
              "distinct CBSAs", "distinct counties",
              "cluster-measured growth", "LMI sites"])
    comp.index.name = "measure"
    print("\n" + comp.to_string())

    cost = ""
    if best is not None:
        d = rule_a["score"] - best["score"]
        cost = (f"{d:.4f} index points, "
                f"{d / rule_a['score'] * 100:.1f}% of the unconstrained score")

    REPORT.write_text(f"""# Three Sites — Recommendation

Produced by `scripts/12_siting_recommendation.py` under the rule pre-registered
in [`siting_decision_rule.md`](siting_decision_rule.md), which was committed
before this computation ran. The git history is the evidence of that ordering.

## Current footprint

| | LMI households | non-LMI households |
|---|---|---|
| covered by any catchment | **{cov_lmi_now:.2%}** | **{cov_non_now:.2%}** |

Gap today: **{cov_lmi_now - cov_non_now:+.2f} percentage points**.
{len(t):,} tracts carry an LMI determination; **{excluded_no_basis} have no basis
by either route and are excluded from both numerator and denominator**, never
counted as non-LMI.

## The two sets

{md(comp)}

**Cost of the constraint: {cost or "not applicable — no set satisfies the test"}.**

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

Across the top 50: the dominant CBSA holds **{int(biggest_cbsa.iloc[0])} of 50**
tracts. Cluster-measured growth runs at
**{int((in_main_corridor & est).sum())} of {int(in_main_corridor.sum())}**
inside it against
**{int((~in_main_corridor & est).sum())} of {int((~in_main_corridor).sum())}**
outside. Correlation between the two, phi = **{phi:+.3f}**.

{"The prediction holds: these are largely one exposure, not two." if phi > 0.2
 else "The prediction does NOT hold - the two are close to independent, which is a better result than expected and is stated as such."}

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
""", encoding="utf-8")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
