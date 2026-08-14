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

THE COMPONENT Z-SCORES SHIP IN LONG FORM ON PURPOSE. FR-03 asks that index
weights be adjustable without rebuilding the model. A what-if parameter over
tidy z-scores turns that from a claim into something a reviewer can drag a
slider and watch. Contributions (weight x z) cannot do this - the weight is
already baked in.
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
    exports["dim_branch"] = con.execute("""
        SELECT uninumbr, cert, institution_name, address, city, state,
               county_fips, latitude, longitude, tract_geoid, service_type,
               is_main_office, first_year, last_year, drift_kind, validity,
               is_subject_bank
        FROM dim_branch
    """).df()
    exports["dim_tract"] = con.execute("""
        SELECT tract_geoid, county_fips, county_name, cbsa, cbsa_title, tier,
               population, households, household_growth_pct, growth_basis,
               median_hh_income, median_family_income, median_home_value,
               tract_to_area_income_pct, lmi_flag, lmi_basis, tract_status,
               centroid_lat, centroid_lon
        FROM dim_tract
    """).df()
    exports["dim_year"] = con.execute("SELECT * FROM dim_year").df()
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
    exports["branch_performance"] = q("SQL-08_branch_performance_index.sql")
    exports["tract_capture_rate"] = q("SQL-10_capture_rate_by_tract.sql")
    exports["unmet_demand"] = q("SQL-11_unmet_demand_ranking.sql")
    exports["lmi_coverage"] = q("SQL-13_lmi_coverage_ac06.sql")

    idx = q("SQL-12_market_opportunity_index.sql")
    exports["opportunity_index"] = idx

    weights = con.execute("SELECT * FROM ref_index_weights").df()
    exports["ref_index_weights"] = weights

    # --- component z-scores, long form, for the what-if parameter ---------
    # Recovered from SQL-12's contributions rather than recomputed, for the
    # same reason script 11 does it: this file cannot then normalise
    # differently from the query it is exporting.
    primary = weights[weights["scenario"] == "primary"].set_index("component")["weight"]
    scored = idx[idx["opportunity_score"].notna()]
    long = pd.concat([
        pd.DataFrame({
            "tract_geoid": scored["tract_geoid"],
            "component": c,
            "z_score": (scored[f"contrib_{c}"] / primary[c]).round(6),
            "default_weight": primary[c],
        }) for c in COMPONENTS
    ], ignore_index=True)

    check = (long.pivot(index="tract_geoid", columns="component",
                        values="z_score") * primary).sum(axis=1)
    err = float((check - scored.set_index("tract_geoid")["opportunity_score"])
                .abs().max())
    if err > 1e-3:
        raise SystemExit(f"Z-scores do not reproduce the published score "
                         f"(max error {err}). Refusing to export them.")
    print(f"z-scores reproduce opportunity_score to {err:.2e}")
    exports["fact_index_components"] = long

    # --- the recommendation ------------------------------------------------
    exports["recommended_sites"] = pd.read_csv(
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
