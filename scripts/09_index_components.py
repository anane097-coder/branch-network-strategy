"""Build the opportunity-index components that need computing, not just reading.

    python scripts/09_index_components.py

Two of the five index components had no source in the warehouse and two more
needed reformulating after the scale test in docs/acceptance_criteria_audit.md.
This script builds the two that need geometry or aggregation:

    competitor_saturation   competitor branches near a tract, over CATCHMENT
                            households - the denominator matched to the
                            numerator's grain
    deposit_market_growth   county deposit CAGR on a RETAIL basis, with
                            booking centres excluded

median_income and household_growth already live in dim_tract; unmet mortgage
demand comes from SQL-11. This script does not normalise, weight, or combine
anything - that is SQL-12's job, and keeping it separate is what lets script
11 vary weights over the adjusted components rather than the raw ones.

WHAT THIS SCRIPT DELIBERATELY DOES NOT BUILD
--------------------------------------------
Not a catchment bridge for competitors. `fact_tract_competition` counts how
many OTHER banks' branches sit within a tract's tier radius. That is a
property of the tract, not of those branches. Competitor branches do not have
catchments here: their tract assignments were never computed, their service
types were never audited the way the subject's were, and nothing in this
project has validated their coordinates. A query joining through this table as
though it were `bridge_branch_catchment` would be asking a question the data
cannot answer, which is why the table is named for the tract and carries no
branch identifier at all.
"""
import importlib.util
import sys
from pathlib import Path

import geopandas
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "staging"
CONFIG = ROOT / "config" / "catchment.yaml"
REPORT = ROOT / "docs" / "index_components.md"
COMPETITION_OUT = STAGING / "fact_tract_competition.csv"
GROWTH_OUT = STAGING / "fact_county_deposit_growth.csv"

SUBJECT_CERT = "5296"
M = 1609.344                    # metres per mile
GROWTH_FROM, GROWTH_TO = 2019, 2025
BOOKING_MULTIPLE = 20           # per SQL-08: above 20x the median, a branch is
                                # doing something other than serving a catchment
FULL_SERVICE = ("11", "12")     # brick-and-mortar and retail; limited-service
                                # facilities structurally book no deposits


def load_radius_rule():
    """Import script 08's rule rather than restating it.

    THE RULE IS THE ARTIFACT AND THERE MUST BE ONE COPY OF IT. A second
    implementation here would drift from 08's the first time either changed,
    and the failure would be silent: two radii, both plausible, applied to
    different components of the same index.
    """
    spec = importlib.util.spec_from_file_location(
        "catchments08", ROOT / "scripts" / "08_catchments.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["catchments08"] = mod
    spec.loader.exec_module(mod)
    return mod.recompute_radii


def md(df: pd.DataFrame, fmt: str = "{}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [fmt.format(v) for v in r]) + " |")
    return "\n".join(out)


def build_competition(branches, tracts, radii):
    """Competitor branches, and catchment households, per tract.

    The denominator is the fix. Counting branches within radius R of a tract
    centroid is a CATCHMENT-area quantity; dividing it by households in the
    TRACT compares two different geographies. A small tract sits in the same
    catchment as a large one but divides by far fewer households, so the ratio
    inflates mechanically - pooled rank correlation -0.404 against tract size,
    and a sevenfold gradient across household deciles that was ranking tract
    size rather than saturation.
    """
    b = branches[branches["BRSERTYP"].astype("string").isin(FULL_SERVICE)
                 & (branches["validity"] == "ok")
                 & branches["SIMS_LATITUDE"].notna()].copy()
    b["is_subject"] = b["CERT"].astype("string") == SUBJECT_CERT

    t = tracts[tracts["centroid_lat"].notna()].copy()

    bg = geopandas.GeoDataFrame(b, geometry=geopandas.points_from_xy(
        b["SIMS_LONGITUDE"], b["SIMS_LATITUDE"]),
        crs="EPSG:4326").to_crs("EPSG:5070")
    tg = geopandas.GeoDataFrame(t, geometry=geopandas.points_from_xy(
        t["centroid_lon"], t["centroid_lat"]),
        crs="EPSG:4326").to_crs("EPSG:5070")
    bx = np.c_[bg.geometry.x, bg.geometry.y]
    tx = np.c_[tg.geometry.x, tg.geometry.y]

    # The radius belongs to the TRACT's tier here, not to a branch's. The
    # question is "what does a branch serving this tract face", so the tier
    # that sets the distance is the tract's own.
    lim = t["tier"].map(radii).to_numpy()[:, None]
    is_comp = (~b["is_subject"]).to_numpy()[None, :]
    hh = t["households"].fillna(0).to_numpy()

    n = len(t)
    comp = np.zeros(n, dtype="int64")
    subj = np.zeros(n, dtype="int64")
    cat_hh = np.zeros(n, dtype="float64")
    cat_tr = np.zeros(n, dtype="int64")
    for i in range(0, n, 400):                    # chunked: 4.8k x 6.5k
        sl = slice(i, i + 400)
        db = np.linalg.norm(tx[sl, None, :] - bx[None, :, :], axis=-1) / M
        near = db <= lim[sl]
        comp[sl] = (near & is_comp).sum(axis=1)
        subj[sl] = (near & ~is_comp).sum(axis=1)
        # Households within the SAME radius - the matched denominator.
        dt = np.linalg.norm(tx[sl, None, :] - tx[None, :, :], axis=-1) / M
        within = dt <= lim[sl]
        cat_hh[sl] = (within * hh[None, :]).sum(axis=1)
        cat_tr[sl] = within.sum(axis=1)

    out = pd.DataFrame({
        "tract_geoid": t["tract_geoid"].to_numpy(),
        "tier": t["tier"].to_numpy(),
        "radius_miles": t["tier"].map(radii).to_numpy().round(2),
        "competitor_branches": comp,
        "subject_branches": subj,
        "catchment_tracts": cat_tr,
        "catchment_households": cat_hh.round(0),
        "tract_households": t["households"].to_numpy(),
    })
    # NULL, not zero, where there is no denominator. A tract whose catchment
    # holds no households has no saturation to measure; zero would read as
    # "no competition" and rank it as open territory.
    denom = out["catchment_households"].replace(0, np.nan)
    out["competitor_per_10k_catchment_hh"] = (
        out["competitor_branches"] / denom * 10_000).round(4)
    # Kept ONLY as the pre-correction series, so the reformulation stays
    # auditable. Never rank on it - see the docstring.
    tdenom = out["tract_households"].replace(0, np.nan)
    out["competitor_per_10k_tract_hh_UNADJUSTED"] = (
        out["competitor_branches"] / tdenom * 10_000).round(4)
    return out


def build_county_growth(sod, branches):
    """County deposit CAGR, total and retail.

    RETAIL, NOT TOTAL, IS THE COMPONENT. County deposit growth on all
    deposits is dominated by booking centres and institution-level events, and
    the two most extreme counties in this footprint are both artifacts:
    McLean IL reads -16.9% because State Farm Bank held $11.39bn across two
    branches in 2020 and is absent from 2021, and Brown WI reads +12.3%
    because the SUBJECT books its own HQ deposits there. The second is the
    same circularity that forced HQ exclusion in BQ-1, arriving through a
    component.

    Exclusion is not the answer - it would drop the two most important
    counties in the footprint, including the bank's home market. Reformulating
    to a retail basis keeps them and measures what a siting decision actually
    depends on. BOTH SERIES ARE RETURNED so the contamination stays visible.
    """
    s = sod[sod["YEAR"].isin([GROWTH_FROM, GROWTH_TO])].copy()
    s["UNINUMBR"] = s["UNINUMBR"].astype("string")
    # DEPSUMBR IS THE BRANCH FIELD. DEPSUM IS THE INSTITUTION'S TOTAL and is
    # populated only on the main-office row - zero on 90% of rows, with
    # DEPDOM repeating the institution total on every one. Using DEPSUM here
    # would have built a "retail" series out of main offices alone, which is
    # precisely the population this reformulation exists to remove. It failed
    # loudly only because the null-before-arithmetic guard refused every
    # county rather than dividing by zero: excluding main offices left rows
    # summing to nothing, so cagr_pct_retail came back NA across the board.
    # Kept in thousands, as SOD reports it; a CAGR is scale-free.
    s["branch_deposits_k"] = pd.to_numeric(s["DEPSUMBR"], errors="coerce")

    # Only geography comes from dim_branch. BKMO is taken from the SOD row
    # itself because main-office status is a fact about the VINTAGE - a
    # branch can become or stop being one - and dim_branch carries the
    # latest value only. Using the latest would apply 2025's designation to
    # 2019's deposits, which is the same grain error as validating a
    # per-vintage field on a latest-only view.
    b = branches[["UNINUMBR", "geo_county", "sod_county"]].copy()
    b["UNINUMBR"] = b["UNINUMBR"].astype("string")
    s = s.merge(b, on="UNINUMBR", how="left", validate="many_to_one")
    # Coordinate-derived county where available - the same choice SQL-01 made.
    s["county_fips"] = s["geo_county"].fillna(s["sod_county"])
    s = s[s["county_fips"].notna()]

    # The booking-concentration threshold needs a stated population, and it is
    # NOT the one SQL-08 used. That index covers the subject alone, so its
    # median is the subject's median. Market growth covers every institution
    # in the footprint, so the median must too - applying the subject's median
    # to a market of 699 institutions would flag on the wrong scale.
    med = s.loc[s["YEAR"] == GROWTH_TO, "branch_deposits_k"].median()
    s["is_main_office"] = pd.to_numeric(s["BKMO"], errors="coerce") == 1
    s["is_booking_concentration"] = s["branch_deposits_k"] > BOOKING_MULTIPLE * med

    # WHICH FLAG IS THE BOOKING CENTRE? is_main_office is NOT one, and using
    # it as a proxy is the wrong-population error again. It means "this is the
    # institution's head office", not "this books non-retail deposits" - and
    # for a community bank the head office IS the retail branch. 480 of the
    # 496 main offices here sit BELOW the concentration threshold, with a
    # median deposit of $118m against a $67m all-branch median: 1.75x, not
    # 20x. They hold 9.9% of footprint deposits and are ordinary retail.
    #
    # The evidence that this matters, not just a definitional quibble:
    #
    #   rule                       McLean IL   Brown WI   rank corr vs total
    #   total (contaminated)         -16.87     +12.32           -
    #   concentration only            +6.80      +2.61        +0.897
    #   concentration + main office   +6.46      +5.24        +0.449
    #
    # Concentration alone fixes BOTH known artifacts while preserving the
    # ranking structure. Adding main offices reshuffles the whole footprint
    # (+0.449) - and that reshuffling is 480 ordinary branches being deleted,
    # not booking distortion being removed.
    s["is_booking_centre"] = s["is_booking_concentration"]

    def agg(frame, label):
        g = frame.groupby(["county_fips", "YEAR"])["branch_deposits_k"].sum().unstack()
        n = frame.groupby(["county_fips", "YEAR"]).size().unstack()
        out = pd.DataFrame({
            f"deposits_{GROWTH_FROM}_{label}": g.get(GROWTH_FROM),
            f"deposits_{GROWTH_TO}_{label}": g.get(GROWTH_TO),
            f"branches_{GROWTH_FROM}_{label}": n.get(GROWTH_FROM),
            f"branches_{GROWTH_TO}_{label}": n.get(GROWTH_TO),
        })
        d0 = out[f"deposits_{GROWTH_FROM}_{label}"]
        d1 = out[f"deposits_{GROWTH_TO}_{label}"]
        span = GROWTH_TO - GROWTH_FROM
        # NULL before the arithmetic. A county with no 2019 base has no growth
        # rate; letting it fall out of the division would give inf or a
        # fabricated number and it would rank.
        ok = d0.notna() & d1.notna() & (d0 > 0)
        cagr = pd.Series(pd.NA, index=out.index, dtype="Float64")
        cagr[ok] = (((d1[ok] / d0[ok]) ** (1 / span) - 1) * 100).round(4)
        out[f"cagr_pct_{label}"] = cagr
        return out

    total = agg(s, "total")
    retail = agg(s[~s["is_booking_centre"]], "retail")
    # The stricter rule, kept as a column rather than argued about. Nobody has
    # to re-run the script to see what the other choice would have produced.
    strict = agg(s[~(s["is_booking_centre"] | s["is_main_office"])], "excl_hq")
    out = total.join(retail, how="outer").join(strict, how="outer")

    # EXCLUSIONS ARE REPORTED PER YEAR, BOTH YEARS. A CAGR has two endpoints
    # and an exclusion at either one moves it. Reporting the end year alone
    # showed McLean County losing ZERO branches beside a +23pp shift, because
    # the branches that mattered - State Farm Bank's two, holding $11.39bn -
    # were in the 2019 base and gone by 2025. "Nothing was excluded" next to a
    # 23-point move is worse than no diagnostic at all.
    for yr in (GROWTH_FROM, GROWTH_TO):
        e = (s[s["is_booking_centre"] & (s["YEAR"] == yr)]
             .groupby("county_fips")
             .agg(**{f"excluded_branches_{yr}": ("UNINUMBR", "size"),
                     f"excluded_deposits_{yr}": ("branch_deposits_k", "sum")}))
        out = out.join(e, how="left")
        out[f"excluded_branches_{yr}"] = out[f"excluded_branches_{yr}"].fillna(0)
        out[f"excluded_deposits_{yr}"] = out[f"excluded_deposits_{yr}"].fillna(0)
        out[f"excluded_branch_share_{yr}"] = (
            out[f"excluded_branches_{yr}"]
            / out[f"branches_{yr}_total"].replace(0, np.nan)).round(4)
        out[f"excluded_deposit_share_{yr}"] = (
            out[f"excluded_deposits_{yr}"]
            / out[f"deposits_{yr}_total"].replace(0, np.nan)).round(4)
    # The headline is whichever endpoint lost more - that is the one carrying
    # the risk, whichever year it happens to be.
    out["excluded_branches"] = out[[f"excluded_branches_{GROWTH_FROM}",
                                    f"excluded_branches_{GROWTH_TO}"]].max(axis=1)
    out["excluded_deposit_share"] = out[
        [f"excluded_deposit_share_{GROWTH_FROM}",
         f"excluded_deposit_share_{GROWTH_TO}"]].max(axis=1)
    out["excluded_branch_share"] = out[
        [f"excluded_branch_share_{GROWTH_FROM}",
         f"excluded_branch_share_{GROWTH_TO}"]].max(axis=1)

    # HOW MUCH OF THE COUNTY WAS REMOVED. A county whose figure is computed
    # after removing three of five branches is a far weaker measurement than
    # one removing one of forty, and a 25% index weight should not be resting
    # on the difference without anybody being able to see it.
    out["cagr_shift_pp"] = (out["cagr_pct_retail"]
                            - out["cagr_pct_total"]).round(4)
    # A county with nothing left after exclusion is refused, not zeroed.
    out["retail_basis_status"] = np.where(
        out["cagr_pct_retail"].notna(), "measured",
        np.where(out[f"branches_{GROWTH_TO}_retail"].fillna(0) == 0,
                 "no_retail_branches", "no_2019_retail_base"))
    return out.reset_index()


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if cfg["radius_rule"] != "median_nearest_neighbour_spacing_within_tier":
        raise SystemExit(f"Unknown radius_rule {cfg['radius_rule']!r}.")
    recompute_radii = load_radius_rule()

    branches = pd.read_csv(STAGING / "dim_branch.csv",
                           dtype={"UNINUMBR": "string", "CERT": "string",
                                  "tract_geoid": "string", "BRSERTYP": "string",
                                  "BKMO": "string", "geo_county": "string",
                                  "sod_county": "string"})
    tracts = pd.read_csv(STAGING / "dim_tract.csv",
                         dtype={"tract_geoid": "string",
                                "county_fips": "string", "cbsa": "string"})
    sod = pd.read_csv(STAGING / "sod_all.csv.gz",
                      dtype={"UNINUMBR": "string", "CERT": "string"},
                      low_memory=False)

    # Radii from the subject's current-vintage branches - the same population
    # script 08 uses, because the radius describes how far a branch of this
    # bank reaches, not how far anybody's branch reaches.
    sb = branches[(branches["CERT"] == SUBJECT_CERT)
                  & (branches["last_year"] == branches["last_year"].max())
                  & branches["tract_geoid"].notna()].merge(
        tracts[["tract_geoid", "tier"]], on="tract_geoid", how="left")
    sb["tier"] = sb["tier"].fillna("rural")
    sbg = geopandas.GeoDataFrame(sb, geometry=geopandas.points_from_xy(
        sb["SIMS_LONGITUDE"], sb["SIMS_LATITUDE"]),
        crs="EPSG:4326").to_crs("EPSG:5070")
    radii, evidence = recompute_radii(
        np.c_[sbg.geometry.x, sbg.geometry.y], sb["tier"].to_numpy(),
        cfg["bounds_miles"])
    print("Radii recomputed via script 08's rule (single implementation):")
    for k, v in evidence.items():
        print(f"  {k:6s} {v['median_nn_miles']:>5.2f} mi  (n={v['branches']})")

    comp = build_competition(branches, tracts, radii)
    comp.to_csv(COMPETITION_OUT, index=False)
    print(f"\nfact_tract_competition: {len(comp):,} tracts")
    print(f"  competitor branches per tract: median "
          f"{comp['competitor_branches'].median():.0f}, "
          f"max {comp['competitor_branches'].max():,}")
    print(f"  tracts with no measurable saturation (no catchment households): "
          f"{comp['competitor_per_10k_catchment_hh'].isna().sum()}")

    growth = build_county_growth(sod, branches)
    growth.to_csv(GROWTH_OUT, index=False)
    print(f"\nfact_county_deposit_growth: {len(growth):,} counties")
    print(growth["retail_basis_status"].value_counts().to_string())
    heavy = growth[growth["excluded_branch_share"] > 0.25]
    print(f"  counties losing >25% of branches to exclusion: {len(heavy)}")
    if len(heavy):
        print(heavy[["county_fips", "excluded_branches",
                     f"branches_{GROWTH_TO}_total", "cagr_pct_total",
                     "cagr_pct_retail"]].to_string(index=False))

    # --- report ---------------------------------------------------------
    band = comp.copy()
    band["hh_decile"] = pd.qcut(band["tract_households"].replace(0, np.nan),
                                10, labels=False, duplicates="drop")
    shape = band.groupby("hh_decile").agg(
        tract_households=("tract_households", "median"),
        catchment_households=("catchment_households", "median"),
        competitor_branches=("competitor_branches", "median"),
        per_10k_tract=("competitor_per_10k_tract_hh_UNADJUSTED", "median"),
        per_10k_catchment=("competitor_per_10k_catchment_hh", "median"),
    ).round(2)

    radii_tbl = pd.DataFrame({
        "median_nn_miles": {k: v["median_nn_miles"] for k, v in evidence.items()},
        "branches": {k: v["branches"] for k, v in evidence.items()},
    })
    moved = growth.dropna(subset=["cagr_shift_pp"]).reindex(
        growth["cagr_shift_pp"].abs().sort_values(ascending=False).index).head(10)
    moved_tbl = moved[["county_fips", "cagr_pct_total", "cagr_pct_retail",
                       "cagr_pct_excl_hq", "cagr_shift_pp",
                       f"excluded_branches_{GROWTH_FROM}",
                       f"excluded_branches_{GROWTH_TO}",
                       "excluded_deposit_share"]].set_index("county_fips")

    REPORT.write_text(f"""# Index Components Built Outside the Warehouse

Generated by `scripts/09_index_components.py`. Two of the five opportunity
index components had no source in the warehouse and two needed reformulating
after the scale test in `acceptance_criteria_audit.md`. Neither is normalised
or weighted here — that is SQL-12's job.

## Radii

Recomputed by importing `recompute_radii` from script 08 rather than restating
the rule. One implementation, so the two cannot drift apart.

{md(radii_tbl)}

## `fact_tract_competition` — and what it is not

{len(comp):,} tracts. Counts how many **other** banks' full-service branches
sit within the tract's tier radius of its centroid.

**This is not a catchment bridge for competitors.** It carries no branch
identifier, because competitor branches do not have catchments in this
project: their tract assignments were never computed, their service types were
never audited as the subject's were, and nothing here has validated their
coordinates. A query joining through this table as though it were
`bridge_branch_catchment` would be asking a question the data cannot answer.

### The denominator is the reformulation

Counting branches within radius *R* is a catchment-area quantity. Dividing it
by households **in the tract** compares two different geographies, and a small
tract sitting in the same catchment as a large one divides by far fewer
households. The ratio then ranks tract size rather than saturation.

{md(shape)}

`per_10k_tract` is retained as `competitor_per_10k_tract_hh_UNADJUSTED` purely
so the correction is auditable. It must not be ranked on.

## `fact_county_deposit_growth` — retail basis

{len(growth):,} counties, both series reported. A branch is excluded from the
retail figure if it is a main office (`BKMO`) or holds more than
{BOOKING_MULTIPLE}× the footprint-wide median branch deposit.

The threshold's population is **every institution in the footprint**, not the
subject alone. SQL-08 uses the subject's own median because that index covers
the subject only; applying it to a market of 699 institutions would flag on
the wrong scale.

Counties where the basis moves the answer most:

{md(moved_tbl)}

Exclusion share is reported per county because a figure computed after
removing three of five branches is a far weaker measurement than one removing
one of forty — and this component carries 25% of the index weight.

Status: {dict(growth["retail_basis_status"].value_counts())}
""", encoding="utf-8")

    print(f"\nWrote {COMPETITION_OUT.relative_to(ROOT)}")
    print(f"Wrote {GROWTH_OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: SQL-12 assembles these into the opportunity index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
