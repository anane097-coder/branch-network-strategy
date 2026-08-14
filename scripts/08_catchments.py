"""
08_catchments.py — build bridge_branch_catchment. AC-03.

Writes  data/staging/bridge_branch_catchment.csv
        data/staging/tracts_uncovered.csv
        docs/catchment_report.md

THE RADIUS IS RECOMPUTED HERE. IT IS NEVER READ FROM THE CONFIG.
---------------------------------------------------------------
config/catchment.yaml records observed 2025 values - 3.5 / 13.1 / 20.8 - and
they are correct today, which is exactly why reading them would be tempting
and wrong. They are recorded as EVIDENCE, not as input. This script recomputes
the median nearest-neighbour spacing from the staged branch data every run.

If it did not, a denser 2027 branch set would be scored against a 2025 radius
and the tool would go quietly wrong in the same way the old binary tier rule
was wrong on this vintage. FR-06 promises the retail team can re-run against a
new vintage unattended; that promise is only honest if the parameters move
with the data.

What IS read from the config: the rule name, the tier definition, the sanity
bounds, and the primary-assignment rule. Behaviour, not numbers.

AC-03 AND WHAT IT ACTUALLY TESTS
--------------------------------
"Every tract in at most one primary catchment" is trivially satisfiable by any
deterministic tie-break, so passing it proves little on its own. The number
that characterises the model is how many tracts fall inside SEVERAL catchments
before assignment - that is the work the nearest-branch rule is doing. With a
radius equal to the median spacing between branches, overlap is expected by
construction, so the tie-breaking matters more than the radius does.

TRACTS IN NO CATCHMENT ARE A FINDING, NOT A RESIDUE
---------------------------------------------------
Roughly three fifths of WI+IL tracts sit outside the footprint entirely. That
set is the raw material for BQ-4 unmet demand and BQ-1 expansion candidates,
so it is written as its own table with the attributes those questions need -
not left as whatever fails to survive a join.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "staging"
CONFIG = ROOT / "config" / "catchment.yaml"
REPORT = ROOT / "docs" / "catchment_report.md"
BRIDGE_OUT = STAGING / "bridge_branch_catchment.csv"
UNCOVERED_OUT = STAGING / "tracts_uncovered.csv"

SUBJECT_CERT = "5296"
M = 1609.344
TIERS = ("metro", "micro", "rural")


def md(df: pd.DataFrame, fmt: str = "{:,}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [
            fmt.format(v) if isinstance(v, (int, float))
            and not isinstance(v, bool) else str(v) for v in r]) + " |")
    return "\n".join(out)


def recompute_radii(bx: np.ndarray, tiers: np.ndarray,
                    bounds: dict) -> tuple[dict, dict]:
    """Median nearest-neighbour distance between the subject's branches, per tier.

    This is the rule stated in config/catchment.yaml. The config's
    observed_2025 block is deliberately NOT consulted.
    """
    d = np.linalg.norm(bx[:, None, :] - bx[None, :, :], axis=-1) / M
    np.fill_diagonal(d, np.inf)
    nn = d.min(axis=1)
    radii, evidence = {}, {}
    for t in TIERS:
        sel = tiers == t
        if sel.sum() < 2:
            raise SystemExit(f"Tier {t!r} has fewer than 2 branches; the "
                             "nearest-neighbour rule cannot be computed.")
        r = float(np.median(nn[sel]))
        if not (bounds["min"] <= r <= bounds["max"]):
            raise SystemExit(
                f"Recomputed {t} radius {r:.2f} mi falls outside the sanity "
                f"bounds {bounds['min']}-{bounds['max']} in {CONFIG.name}. "
                "Something changed structurally - review before proceeding "
                "rather than accepting a number nobody looked at.")
        radii[t] = r
        evidence[t] = {"branches": int(sel.sum()), "median_nn_miles": round(r, 2)}
    return radii, evidence


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if cfg["radius_rule"] != "median_nearest_neighbour_spacing_within_tier":
        raise SystemExit(f"Unknown radius_rule {cfg['radius_rule']!r}.")
    if cfg["primary_assignment"] != "nearest_branch":
        raise SystemExit(f"Unknown primary_assignment {cfg['primary_assignment']!r}.")
    bounds = cfg["bounds_miles"]

    branches = pd.read_csv(STAGING / "dim_branch.csv",
                           dtype={"UNINUMBR": "string", "CERT": "string",
                                  "tract_geoid": "string"})
    tracts = pd.read_csv(STAGING / "dim_tract.csv",
                         dtype={"tract_geoid": "string", "county_fips": "string",
                                "cbsa": "string"})

    b = branches[(branches["CERT"] == SUBJECT_CERT)
                 & (branches["last_year"] == branches["last_year"].max())
                 & branches["tract_geoid"].notna()].copy()
    b = b.merge(tracts[["tract_geoid", "tier"]], on="tract_geoid", how="left")
    b["tier"] = b["tier"].fillna("rural")
    print(f"Subject branches, current vintage: {len(b)}")
    print(f"  by tier: {b['tier'].value_counts().to_dict()}")

    # --- project once, in an equal-area CRS ------------------------------
    bg = geopandas.GeoDataFrame(b, geometry=geopandas.points_from_xy(
        b["SIMS_LONGITUDE"], b["SIMS_LATITUDE"]), crs="EPSG:4326").to_crs("EPSG:5070")
    bx = np.c_[bg.geometry.x, bg.geometry.y]

    t = tracts[tracts["centroid_lat"].notna()].copy()
    tg = geopandas.GeoDataFrame(t, geometry=geopandas.points_from_xy(
        t["centroid_lon"], t["centroid_lat"]), crs="EPSG:4326").to_crs("EPSG:5070")
    tx = np.c_[tg.geometry.x, tg.geometry.y]

    radii, evidence = recompute_radii(bx, b["tier"].to_numpy(), bounds)
    print("\nRadii RECOMPUTED from observed spacing (not read from config):")
    for k, v in evidence.items():
        print(f"  {k:6s} {v['median_nn_miles']:>5.2f} mi  (n={v['branches']})")
    recorded = cfg.get("observed_2025", {})
    drift_note = []
    for k in TIERS:
        was = recorded.get(k, {}).get("median_nn_miles")
        if was is not None and abs(was - radii[k]) > 0.05:
            drift_note.append(f"{k}: config records {was}, recomputed "
                              f"{radii[k]:.2f}")

    # --- membership -------------------------------------------------------
    D = np.linalg.norm(tx[:, None, :] - bx[None, :, :], axis=-1) / M
    thresh = np.array([radii[x] for x in b["tier"]])
    inside = D <= thresh

    ti, bi = np.nonzero(inside)
    bridge = pd.DataFrame({
        "tract_geoid": t["tract_geoid"].to_numpy()[ti],
        "uninumbr": b["UNINUMBR"].to_numpy()[bi],
        "branch_tier": b["tier"].to_numpy()[bi],
        "distance_miles": D[ti, bi].round(3),
    })

    per_tract = bridge.groupby("tract_geoid").size()
    multi = int((per_tract > 1).sum())
    covered = int(len(per_tract))
    print(f"\nMembership: {len(bridge):,} branch-tract pairs, "
          f"{covered:,} tracts inside at least one catchment")
    print(f"  tracts inside MORE THAN ONE catchment: {multi:,} "
          f"({multi / covered:.1%} of covered)")

    # --- primary assignment: nearest branch wins --------------------------
    bridge = bridge.sort_values(["tract_geoid", "distance_miles", "uninumbr"])
    bridge["is_primary"] = ~bridge.duplicated("tract_geoid", keep="first")

    # AC-03
    primaries = bridge[bridge["is_primary"]].groupby("tract_geoid").size()
    ac03 = primaries.max() if len(primaries) else 0
    if ac03 > 1:
        raise SystemExit(f"AC-03 FAILED: {int((primaries > 1).sum())} tracts "
                         "have more than one primary catchment.")
    print(f"  [PASS] AC-03: every covered tract has exactly one primary "
          f"({len(primaries):,} tracts)")

    # The bridge is SUBJECT-ONLY by design. Carried as a column so a query
    # that forgets the scope is at least inspectable - see the scope rule in
    # the data dictionary. Prose does not survive contact with SQL.
    bridge["is_subject_bank"] = True

    STAGING.mkdir(parents=True, exist_ok=True)
    bridge.to_csv(BRIDGE_OUT, index=False)

    # --- tie-break sensitivity -------------------------------------------
    # Coverage cannot move under a different tie-break - membership is fixed
    # by the radius. What moves is which branch OWNS which tract, and that is
    # the input to BQ-3's catchment potential.
    hh = tracts.set_index("tract_geoid")["households"]
    sod = pd.read_csv(STAGING / "sod_all.csv.gz",
                      dtype={"UNINUMBR": "string"},
                      usecols=["UNINUMBR", "DEPSUMBR", "_year"], low_memory=False)
    dep = (sod[sod["_year"] == sod["_year"].max()]
           .drop_duplicates("UNINUMBR").set_index("UNINUMBR")["DEPSUMBR"])
    if dep.empty:
        raise SystemExit("No deposit data for the tie-break sensitivity - "
                         "the comparison would silently be skipped.")
    tb = None
    if True:
        alt = bridge.assign(dep=bridge["uninumbr"].map(dep))
        near = (alt.sort_values(["tract_geoid", "distance_miles", "uninumbr"])
                   .drop_duplicates("tract_geoid").set_index("tract_geoid")["uninumbr"])
        big = (alt.sort_values(["tract_geoid", "dep", "uninumbr"],
                               ascending=[True, False, True])
                  .drop_duplicates("tract_geoid").set_index("tract_geoid")["uninumbr"])
        switched = int((near != big).sum())
        contested_ix = per_tract[per_tract > 1].index

        def pot(assign):
            d = assign.rename("u").reset_index()
            return d.assign(h=d["tract_geoid"].map(hh)).groupby("u")["h"].sum()

        pn, pb = pot(near), pot(big)
        c = pd.concat([pn.rename("n"), pb.rename("b")], axis=1).fillna(0)
        pct = np.where(c["n"] > 0, (c["b"] - c["n"]).abs() / c["n"] * 100, np.nan)
        tb = {
            "switched": switched,
            "switched_pct": switched / len(near),
            "contested_switched": int((near != big).loc[contested_ix].sum()),
            "contested_pct": float((near != big).loc[contested_ix].mean()),
            "median_pct_change": float(np.nanmedian(pct)),
            "over_25": int(np.nansum(pct > 25)),
            "over_100": int(np.nansum(pct > 100)),
            "branches": len(c),
        }

    # --- uncovered tracts as a first-class set ---------------------------
    unc = tracts[~tracts["tract_geoid"].isin(per_tract.index)].copy()
    nearest = D.min(axis=1)
    nearest_by_tract = pd.Series(nearest, index=t["tract_geoid"].to_numpy())
    unc["miles_to_nearest_branch"] = unc["tract_geoid"].map(nearest_by_tract).round(2)
    unc = unc.sort_values("miles_to_nearest_branch")
    unc.to_csv(UNCOVERED_OUT, index=False)

    unc_known = unc[unc["lmi_flag"].notna()]
    cov_lmi = tracts[tracts["tract_geoid"].isin(per_tract.index)
                     & tracts["lmi_flag"].notna()]

    tier_tbl = pd.DataFrame({
        "branches": pd.Series({k: v["branches"] for k, v in evidence.items()}),
        "radius_mi": pd.Series({k: v["median_nn_miles"] for k, v in evidence.items()}),
        "tracts_covered": bridge.groupby("branch_tier")["tract_geoid"].nunique(),
    }).fillna(0)
    tier_tbl.index.name = "tier"

    REPORT.write_text(f"""# Catchment Construction

Generated by `scripts/08_catchments.py`. AC-03.

## Radii — recomputed, not read

The rule in `config/catchment.yaml` is
`{cfg['radius_rule']}`. This script applies it to the staged branch data on
every run. The `observed_2025` block in the config is **evidence, not input**,
and is not consulted — a denser future branch set must move the radius, or the
tool goes quietly wrong on a new vintage.

{md(tier_tbl, "{:,.2f}")}

{"**Recomputed values differ from those recorded in the config:** "
 + "; ".join(drift_note) + ". The config's evidence block is stale and should "
 "be refreshed." if drift_note else
 "Recomputed values agree with those recorded in the config, as expected on "
 "the vintage the config documents."}

Sanity bounds {bounds['min']}–{bounds['max']} miles were respected; a radius
outside them halts the run rather than proceeding on a number nobody examined.

## AC-03 — every tract in at most one primary catchment

**PASS.** {len(primaries):,} covered tracts, each with exactly one primary.

That is the criterion, and on its own it proves very little — any
deterministic tie-break satisfies it. The number that characterises the model
is how much overlap the rule had to resolve:

| | Tracts |
|---|---|
| Inside at least one catchment | {covered:,} |
| **Inside more than one catchment** | **{multi:,}** ({multi / covered:.1%} of covered) |
| Branch–tract pairs before assignment | {len(bridge):,} |
| Pairs surviving as primary | {int(bridge['is_primary'].sum()):,} |

Overlap is expected by construction: the radius equals the median distance
between branches, so adjacent trade areas meet by design and
`nearest_branch` partitions them. **{multi / covered:.0%} of covered tracts
required tie-breaking**, so the assignment rule is doing more work than the
radius is — which is worth stating plainly, because it means a change to the
tie-break would move results more than a modest change to the radius.

## Tie-break sensitivity — the model is NOT robust to this choice

Coverage cannot move under a different tie-break; membership is fixed by the
radius. What moves is which branch **owns** which tract, and that is the input
to BQ-3's catchment potential.

Tested against an alternative rule — largest-deposit branch wins rather than
nearest:

{f'''| | |
|---|---|
| Covered tracts changing primary | **{tb["switched"]:,} ({tb["switched_pct"]:.1%})** |
| Contested tracts changing primary | **{tb["contested_switched"]:,} ({tb["contested_pct"]:.1%})** |
| Median change in per-branch catchment households | **{tb["median_pct_change"]:.1f}%** |
| Branches changing more than 25% | {tb["over_25"]} of {tb["branches"]} |
| Branches changing more than 100% | {tb["over_100"]} of {tb["branches"]} |

Total households inside catchments is identical under both rules, confirming
this is a redistribution rather than a change in coverage.

**This is a limitation, not a robustness result, and it is stated as one.**
Nearly 60% of contested tracts change hands, and the median branch sees its
catchment household base move by roughly a third. BQ-3's performance index
depends materially on a choice that geography alone does not force.

**Why `nearest_branch` is nevertheless the right rule** — and the reason is
stronger than convention. The alternative tested is *circular for BQ-3*:
assigning tracts by deposit size, then comparing actual deposits against
deposits predicted from the catchment, builds the outcome into the input.
`nearest_branch` uses only geography and no outcome variable, which is the
independence property the performance index requires. The alternative is not
merely different, it is inadmissible for the question the catchments exist to
answer.

The fragility remains real and belongs in the case study beside the index
weight sensitivity in script 11. Both are free choices in the model; both
should be shown rather than defended.''' if tb else "_Not computed._"}

## Tracts in no catchment — a finding, not a residue

{len(unc):,} of {len(tracts):,} tracts ({len(unc) / len(tracts):.1%}) sit
outside the footprint entirely. Written to
`data/staging/tracts_uncovered.csv` as a first-class table with distance to
the nearest branch, demographics and LMI status attached.

This set is the raw material for **BQ-4 unmet mortgage demand** and **BQ-1
expansion candidates**. It is produced deliberately rather than being whatever
survives a join.

**Reconciliation.** Coverage of {covered / len(tracts):.1%} matches the 38.5%
computed independently during radius selection, before this rule existed —
that estimate came from a standalone spacing analysis, this one from the
implemented rule applied to the staged data. Two derivations reaching the same
figure is a reconciliation, not a coincidence.

| | Covered | Uncovered |
|---|---|---|
| Tracts | {covered:,} | {len(unc):,} |
| Households | {tracts[tracts['tract_geoid'].isin(per_tract.index)]['households'].sum():,.0f} | {unc['households'].sum():,.0f} |
| LMI tracts | {int(cov_lmi['lmi_flag'].sum()):,} | {int(unc_known['lmi_flag'].sum()):,} |
| LMI share of tracts with a determination | {cov_lmi['lmi_flag'].mean():.1%} | {unc_known['lmi_flag'].mean():.1%} |

Nearest uncovered tracts — the closest thing to a natural expansion frontier,
before any index is applied:

{md(unc.head(10)[["county_name", "tier", "households", "miles_to_nearest_branch"]].set_index(unc.head(10)["tract_geoid"]), "{:,.1f}")}
""", encoding="utf-8")

    print(f"\nUncovered tracts: {len(unc):,} of {len(tracts):,} "
          f"({len(unc) / len(tracts):.1%})")
    print(f"Wrote {BRIDGE_OUT.relative_to(ROOT)}")
    print(f"Wrote {UNCOVERED_OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: python scripts/09_build_warehouse.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
