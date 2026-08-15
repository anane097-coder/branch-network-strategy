"""Export a curated, dashboard-ready model for Power BI.

    python scripts/13_export_powerbi.py

Not a dump of the warehouse. The design is explicit that each page answers
stated questions rather than displaying everything available, so this exports
what the four pages need and nothing else - a model a reviewer can understand
in one screen beats one they have to navigate.

Query outputs are exported rather than raw facts wherever a query already
resolves a definitional choice. `fact_tract_lending` holds 474,707 rows at a
grain that exists so `action_taken` stays visible in SQL; the dashboard needs
capture rates, and shipping the grain would invite someone to re-derive them in
DAX with a different definition of an origination.

THE COMPONENT Z-SCORES SHIP WIDE, ONE COLUMN PER COMPONENT. FR-03 asks that
index weights be adjustable without rebuilding the model, and a what-if
parameter over z-scores turns that from a claim into something a reviewer can
drag a slider and watch. Contributions (weight x z) cannot do this - the
weight is already baked in.

Wide rather than long because spec 11's Weighted Score measure is a plain
SUMX over five named columns. The long form worked but needed a SUMX over
VALUES() with a SWITCH inside, which is harder to read and therefore harder
to check - and this measure is the one a reviewer is most likely to read.
"""
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "branch_analysis.duckdb"
SQL = ROOT / "sql"
OUT = ROOT / "powerbi" / "data"

COMPONENTS = ["household_growth", "median_income", "deposit_market_growth",
              "competitor_saturation", "unmet_mortgage_demand"]


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    OUT.mkdir(parents=True, exist_ok=True)

    def q(name: str) -> pd.DataFrame:
        return con.execute((SQL / name).read_text(encoding="utf-8")).df()

    exports: dict[str, pd.DataFrame] = {}

    # --- dimensions -------------------------------------------------------
    # Performance columns merge ONTO dim_branch, per spec 11, whose measures
    # read AVERAGE(dim_branch[index_size_adjusted]). A separate table would be
    # a second grain for one entity and a relationship that adds nothing.
    #
    # LEFT JOIN, not inner: the index covers 162 subject branches out of
    # 6,467. An inner join would silently drop every competitor branch from
    # the dimension and take the map layer and the market-share denominator
    # with it.
    perf = q("SQL-08_branch_performance_index.sql")
    con.register("perf", perf)
    exports["dim_branch"] = con.execute("""
        SELECT b.uninumbr, b.cert, b.institution_name, b.address, b.city,
               b.state, b.county_fips, b.latitude, b.longitude, b.tract_geoid,
               b.service_type, b.is_main_office, b.first_year, b.last_year,
               b.drift_kind, b.position_drift_miles, b.county_agrees,
               b.validity, b.is_subject_bank,
               p.index_size_adjusted,
               p.performance_index          AS index_raw,
               p.diagnosis, p.market, p.cagr_3y_pct,
               p.actual_deposits, p.predicted_deposits, p.households
                                            AS catchment_households,
               p.booking_concentration, p.catchment_partly_unmeasured
        FROM dim_branch b
        LEFT JOIN perf p ON p.uninumbr = b.uninumbr
    """).df()
    # in_catchment is required by the LMI Coverage measure in spec 11. It is
    # computed here rather than in DAX so the bridge's subject-only scope is
    # resolved once, in a place that says so, rather than inside a measure
    # whose name would have to carry the caveat.
    exports["dim_tract"] = con.execute("""
        SELECT t.tract_geoid, t.county_fips, t.county_name, t.cbsa,
               t.cbsa_title, t.tier, t.population, t.households,
               t.household_growth_pct, t.growth_basis, t.median_hh_income,
               t.median_family_income, t.median_home_value,
               t.tract_to_area_income_pct, t.lmi_flag, t.lmi_basis,
               t.tract_status, t.centroid_lat, t.centroid_lon,
               EXISTS (SELECT 1 FROM bridge_branch_catchment b
                       WHERE b.tract_geoid = t.tract_geoid) AS in_catchment
        FROM dim_tract t
    """).df()
    # A REAL DATE COLUMN, because DATEADD requires one. dim_year holds
    # integers; DATEADD against an integer column does not error, it returns
    # blank, and a year-over-year measure then reads as no change. The date is
    # the SOD as-of date, 30 June, not 1 January - the vintage is a snapshot.
    exports["dim_year"] = con.execute("""
        SELECT year, sod_as_of_date, make_date(year, 6, 30) AS date
        FROM dim_year ORDER BY year
    """).df()
    exports["dim_institution"] = con.execute("""
        SELECT cert, institution_name, lei, match_quality, is_subject_bank
        FROM dim_institution
    """).df()

    # --- facts the pages actually plot ------------------------------------
    exports["fact_branch_deposits"] = con.execute(
        "SELECT uninumbr, cert, year, deposits FROM fact_branch_deposits").df()
    exports["bridge_branch_catchment"] = con.execute(
        "SELECT * FROM bridge_branch_catchment").df()
    exports["fact_tract_competition"] = con.execute("""
        SELECT tract_geoid, tier, radius_miles, competitor_branches,
               subject_branches, catchment_households,
               competitor_per_10k_catchment_hh
        FROM fact_tract_competition
    """).df()
    exports["fact_county_deposit_growth"] = con.execute("""
        SELECT county_fips, cagr_pct_total, cagr_pct_retail, cagr_shift_pp,
               excluded_branches, excluded_deposit_share, retail_basis_status
        FROM fact_county_deposit_growth
    """).df()

    # --- query outputs, where the query IS the definition ------------------
    exports["market_share"] = q("SQL-02_market_share_by_county_year.sql")
    exports["tract_capture_rate"] = q("SQL-10_capture_rate_by_tract.sql")
    exports["unmet_demand"] = q("SQL-11_unmet_demand_ranking.sql")
    exports["lmi_coverage"] = q("SQL-13_lmi_coverage_ac06.sql")

    idx = q("SQL-12_market_opportunity_index.sql")
    exports["opportunity_index"] = idx

    weights = con.execute("SELECT * FROM ref_index_weights").df()
    exports["ref_index_weights"] = weights

    # --- component z-scores, one column each, for the what-if sliders ------
    # Recovered from SQL-12's contributions rather than recomputed, for the
    # same reason script 11 does it: this file cannot then normalise
    # differently from the query it is exporting.
    primary = weights[weights["scenario"] == "primary"].set_index("component")["weight"]
    scored = idx[idx["opportunity_score"].notna()]
    SHORT = {"household_growth": "household_growth_z",
             "median_income": "median_income_z",
             "deposit_market_growth": "deposit_growth_z",
             "competitor_saturation": "competitor_saturation_z",
             "unmet_mortgage_demand": "unmet_demand_z"}
    comp = pd.DataFrame({"tract_geoid": scored["tract_geoid"].to_numpy()})
    for c in COMPONENTS:
        comp[SHORT[c]] = (scored[f"contrib_{c}"] / primary[c]).round(6).to_numpy()
    comp["opportunity_score"] = scored["opportunity_score"].to_numpy()
    comp["county_name"] = scored["county_name"].to_numpy()
    comp["lmi_flag"] = scored["lmi_flag"].to_numpy()
    comp["growth_is_estimated"] = scored["growth_is_estimated"].to_numpy()

    check = sum(comp[SHORT[c]] * primary[c] for c in COMPONENTS)
    err = float((check - comp["opportunity_score"]).abs().max())
    if err > 1e-3:
        raise SystemExit(f"Z-scores do not reproduce the published score "
                         f"(max error {err}). Refusing to export them.")
    print(f"z-scores reproduce opportunity_score to {err:.2e}")
    exports["index_components"] = comp

    # --- the recommendation ------------------------------------------------
    exports["recommendation_sets"] = pd.read_csv(
        ROOT / "data" / "staging" / "ref_recommended_sites.csv",
        dtype={"tract_geoid": "string"})
    cov = pd.read_csv(ROOT / "data" / "staging" / "ref_recommended_coverage.csv",
                      dtype={"tract_geoid": "string"})
    exports["recommended_coverage"] = cov

    # --- write, with a manifest a reviewer can check against ---------------
    rows = []
    for name, df in exports.items():
        path = OUT / f"{name}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        rows.append({"table": name, "rows": len(df), "columns": len(df.columns),
                     "kb": round(path.stat().st_size / 1024, 1)})
        print(f"  {name:28s} {len(df):>8,} rows  {len(df.columns):>3} cols")

    manifest = pd.DataFrame(rows).sort_values("table")
    manifest.to_csv(OUT / "_manifest.csv", index=False)
    total = manifest["kb"].sum()
    print(f"\n{len(exports)} tables, {manifest['rows'].sum():,} rows, "
          f"{total/1024:.1f} MB total")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
