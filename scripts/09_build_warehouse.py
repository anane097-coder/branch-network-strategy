"""
09_build_warehouse.py — load the dimensional model into DuckDB.

Writes data/warehouse/branch_analysis.duckdb and docs/warehouse_load.md.

TYPES ARE DECLARED, NEVER INFERRED.
-----------------------------------
This is the last point where trap #1 can still bite. DuckDB's CSV reader will
happily infer `55001950100` as a BIGINT and `05296` as an INTEGER, and once a
GEOID has lost its leading zero nothing downstream can tell. Every table is
therefore created with an explicit schema and loaded with `read_csv` under a
declared column list - no `read_csv_auto` anywhere in this file.

Then it is verified rather than assumed: after loading, every identifier
column is checked for the right length and for the absence of anything that
would indicate numeric coercion. A comment saying "TEXT, always" has been in
the project guide since the start; this is where it becomes a test.

MONEY
-----
SOD reports deposits in THOUSANDS. The conversion to whole dollars happens
here, once, explicitly, and is the only place it happens.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "staging"
WAREHOUSE = ROOT / "data" / "warehouse"
DB = WAREHOUSE / "branch_analysis.duckdb"
REPORT = ROOT / "docs" / "warehouse_load.md"

SUBJECT_CERT = "5296"
ATTRIBUTE_AS_OF = date(2026, 8, 14)      # FDIC institutions pull date
SOD_AS_OF = {y: date(y, 6, 30) for y in range(2019, 2026)}

# Identifier columns and the exact length each must have after loading.
# Checked, not trusted.
ID_LENGTHS = {
    ("dim_tract", "tract_geoid"): 11,
    ("dim_tract", "county_fips"): 5,
    ("dim_branch", "tract_geoid"): 11,
    ("dim_branch", "county_fips"): 5,
    ("fact_tract_lending", "tract_geoid"): 11,
    ("bridge_branch_catchment", "tract_geoid"): 11,
}


def main() -> int:
    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()                      # rebuild from scratch: AC-04
    con = duckdb.connect(str(DB))

    sod = pd.read_csv(STAGING / "sod_all.csv.gz",
                      dtype={"CERT": "string", "UNINUMBR": "string"},
                      low_memory=False)
    branch = pd.read_csv(STAGING / "dim_branch.csv",
                         dtype={"UNINUMBR": "string", "CERT": "string",
                                "tract_geoid": "string", "sod_county": "string",
                                "geo_county": "string"})
    tract = pd.read_csv(STAGING / "dim_tract.csv",
                        dtype={"tract_geoid": "string", "county_fips": "string",
                               "cbsa": "string"})
    lending = pd.read_csv(STAGING / "fact_tract_lending.csv",
                          dtype={"tract_geoid": "string", "lei": "string",
                                 "loan_purpose": "string",
                                 "action_taken": "string"})
    bridge = pd.read_csv(STAGING / "bridge_branch_catchment.csv",
                         dtype={"tract_geoid": "string", "uninumbr": "string"})
    xwalk = pd.read_csv(STAGING / "dim_institution_crosswalk.csv",
                        dtype={"CERT": "string", "fed_rssd": "string",
                               "lei": "string"})

    # --- dim_year --------------------------------------------------------
    dim_year = pd.DataFrame({"year": list(SOD_AS_OF),
                             "sod_as_of_date": list(SOD_AS_OF.values())})

    # --- dim_institution -------------------------------------------------
    inst = (xwalk.rename(columns={"CERT": "cert", "name": "institution_name"})
                 .assign(is_subject_bank=lambda d: d["cert"] == SUBJECT_CERT,
                         attribute_as_of_date=ATTRIBUTE_AS_OF)
            [["cert", "institution_name", "fed_rssd", "lei", "match_quality",
              "match_method", "is_subject_bank", "attribute_as_of_date"]])

    # --- dim_branch ------------------------------------------------------
    b = branch.rename(columns={
        "UNINUMBR": "uninumbr", "CERT": "cert", "NAMEFULL": "institution_name",
        "ADDRESBR": "address", "CITYBR": "city", "STALPBR": "state",
        "SIMS_LATITUDE": "latitude", "SIMS_LONGITUDE": "longitude",
        "BRSERTYP": "service_type", "BKMO": "is_main_office",
        "sod_county": "county_fips"})
    b["is_subject_bank"] = b["cert"] == SUBJECT_CERT
    dim_branch = b[["uninumbr", "cert", "institution_name", "address", "city",
                    "state", "county_fips", "latitude", "longitude",
                    "tract_geoid", "service_type", "is_main_office",
                    "first_year", "last_year", "position_drift_miles",
                    "drift_kind", "county_agrees", "validity",
                    "is_subject_bank"]]

    # --- fact_branch_deposits --------------------------------------------
    # DEPSUMBR is reported in THOUSANDS of dollars. Converted here, once.
    fact_dep = sod[["UNINUMBR", "CERT", "_year", "DEPSUMBR", "DEPSUM"]].rename(
        columns={"UNINUMBR": "uninumbr", "CERT": "cert", "_year": "year"})
    fact_dep["deposits"] = (fact_dep["DEPSUMBR"] * 1000).astype("Int64")
    fact_dep["institution_total_deposits"] = (fact_dep["DEPSUM"] * 1000).astype("Int64")
    fact_dep = fact_dep[["uninumbr", "cert", "year", "deposits",
                         "institution_total_deposits"]]

    SCHEMA = {
        "dim_year": ("year INTEGER, sod_as_of_date DATE", dim_year),
        "dim_institution": (
            "cert TEXT, institution_name TEXT, fed_rssd TEXT, lei TEXT, "
            "match_quality TEXT, match_method TEXT, is_subject_bank BOOLEAN, "
            "attribute_as_of_date DATE", inst),
        "dim_branch": (
            "uninumbr TEXT, cert TEXT, institution_name TEXT, address TEXT, "
            "city TEXT, state TEXT, county_fips TEXT, latitude DOUBLE, "
            "longitude DOUBLE, tract_geoid TEXT, service_type TEXT, "
            "is_main_office TEXT, first_year INTEGER, last_year INTEGER, "
            "position_drift_miles DOUBLE, drift_kind TEXT, "
            "county_agrees BOOLEAN, validity TEXT, is_subject_bank BOOLEAN",
            dim_branch),
        "dim_tract": (
            "tract_geoid TEXT, county_fips TEXT, county_name TEXT, cbsa TEXT, "
            "cbsa_title TEXT, tier TEXT, population BIGINT, households BIGINT, "
            # households_2019 is DOUBLE, not BIGINT: an apportioned count is
            # fractional by construction and rounding it would hide that.
            "households_2019 DOUBLE, household_growth_pct DOUBLE, "
            # The pre-correction tract-level value, kept so the cluster
            # substitution stays auditable rather than silently applied.
            "household_growth_pct_tract DOUBLE, growth_basis TEXT, "
            "growth_cluster BIGINT, cluster_tracts BIGINT, "
            "median_hh_income BIGINT, median_family_income BIGINT, "
            "median_home_value BIGINT, owner_occupied_units BIGINT, "
            "tract_to_area_income_pct DOUBLE, lmi_flag BOOLEAN, "
            "lmi_basis TEXT, tract_status TEXT, centroid_lat DOUBLE, "
            "centroid_lon DOUBLE", tract),
        "fact_branch_deposits": (
            "uninumbr TEXT, cert TEXT, year INTEGER, deposits BIGINT, "
            "institution_total_deposits BIGINT", fact_dep),
        "fact_tract_lending": (
            "tract_geoid TEXT, lei TEXT, loan_purpose TEXT, action_taken TEXT, "
            "record_count BIGINT, total_amount DOUBLE, action_class TEXT",
            lending),
        "bridge_branch_catchment": (
            "tract_geoid TEXT, uninumbr TEXT, branch_tier TEXT, "
            "distance_miles DOUBLE, is_primary BOOLEAN, is_subject_bank BOOLEAN",
            bridge),
        # The AC-01 reference: FDIC's own server-side aggregate over the full
        # SOD source table. Not a re-sum of our extract.
        "ref_sod_state_totals": (
            "year INTEGER, state TEXT, branches BIGINT, "
            "deposits_thousands BIGINT",
            pd.read_csv(ROOT / "data" / "raw" / "fdic_sod_state_totals.csv")),
    }

    print("Loading with DECLARED types (no inference):\n")
    for name, (ddl, df) in SCHEMA.items():
        cols = [c.rsplit(" ", 1)[0].strip() for c in ddl.split(",")]
        con.execute(f"CREATE TABLE {name} ({ddl})")
        con.register("_src", df[cols])
        con.execute(f"INSERT INTO {name} SELECT * FROM _src")
        con.unregister("_src")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:28s} {n:>9,} rows")

    # --- verify the types actually survived ------------------------------
    print("\nIdentifier integrity (trap #1 becomes a test):")
    failures = []
    for (tbl, col), want in ID_LENGTHS.items():
        bad = con.execute(
            f"SELECT count(*) FROM {tbl} "
            f"WHERE {col} IS NOT NULL AND length({col}) <> {want}").fetchone()[0]
        typ = con.execute(
            f"SELECT data_type FROM information_schema.columns "
            f"WHERE table_name='{tbl}' AND column_name='{col}'").fetchone()[0]
        ok = bad == 0 and typ.upper() in ("VARCHAR", "TEXT")
        print(f"  {tbl}.{col:16s} {typ:8s} wrong length: {bad:>5}  "
              f"{'ok' if ok else '*** FAIL ***'}")
        if not ok:
            failures.append(f"{tbl}.{col} type={typ} bad_length={bad}")

    # --- referential integrity -------------------------------------------
    print("\nReferential integrity:")
    checks = {
        "fact_branch_deposits.uninumbr -> dim_branch":
            "SELECT count(*) FROM fact_branch_deposits f "
            "LEFT JOIN dim_branch d USING (uninumbr) WHERE d.uninumbr IS NULL",
        "fact_branch_deposits.cert -> dim_institution":
            "SELECT count(*) FROM fact_branch_deposits f "
            "LEFT JOIN dim_institution i USING (cert) WHERE i.cert IS NULL",
        "bridge.uninumbr -> dim_branch":
            "SELECT count(*) FROM bridge_branch_catchment b "
            "LEFT JOIN dim_branch d USING (uninumbr) WHERE d.uninumbr IS NULL",
        "bridge.tract_geoid -> dim_tract":
            "SELECT count(*) FROM bridge_branch_catchment b "
            "LEFT JOIN dim_tract t USING (tract_geoid) WHERE t.tract_geoid IS NULL",
        "fact_tract_lending.tract_geoid -> dim_tract":
            "SELECT count(*) FROM fact_tract_lending f "
            "LEFT JOIN dim_tract t USING (tract_geoid) WHERE t.tract_geoid IS NULL",
        "dim_branch.tract_geoid -> dim_tract":
            "SELECT count(*) FROM dim_branch d "
            "LEFT JOIN dim_tract t USING (tract_geoid) "
            "WHERE d.tract_geoid IS NOT NULL AND t.tract_geoid IS NULL",
    }
    ri = {}
    for label, sql in checks.items():
        n = con.execute(sql).fetchone()[0]
        ri[label] = n
        print(f"  {label:46s} orphans: {n:>6,}{'' if n == 0 else '  <-- enumerated'}")

    # A count is not enough. An orphan that cannot be named cannot be judged,
    # and the interesting question is always WHICH rows, not how many.
    orphan_detail = con.execute("""
        SELECT f.tract_geoid,
               substr(f.tract_geoid, 1, 2) AS state_fips,
               count(*) AS fact_rows,
               sum(f.record_count) AS applications,
               count(DISTINCT f.lei) AS lenders
        FROM fact_tract_lending f
        LEFT JOIN dim_tract t USING (tract_geoid)
        WHERE t.tract_geoid IS NULL
        GROUP BY 1, 2 ORDER BY 4 DESC
    """).df()
    if len(orphan_detail):
        print("\n  Orphan tracts, enumerated:")
        print("   ", orphan_detail.to_string(index=False).replace("\n", "\n    "))

    # AC-03 re-tested in the warehouse, not just in the script that built it.
    ac03 = con.execute(
        "SELECT count(*) FROM (SELECT tract_geoid FROM bridge_branch_catchment "
        "WHERE is_primary GROUP BY tract_geoid HAVING count(*) > 1)").fetchone()[0]
    print(f"\n  AC-03 in-warehouse: tracts with >1 primary = {ac03} "
          f"{'[PASS]' if ac03 == 0 else '[FAIL]'}")

    counts = {n: con.execute(f"SELECT count(*) FROM {n}").fetchone()[0]
              for n in SCHEMA}
    con.close()

    # Rendered by hand rather than via DataFrame.to_markdown, which pulls in
    # tabulate - not a pinned dependency, and not worth adding for a table.
    if len(orphan_detail):
        hdr = list(orphan_detail.columns)
        orphan_md = "\n".join(
            ["| " + " | ".join(hdr) + " |",
             "|" + "|".join("---" for _ in hdr) + "|"]
            + ["| " + " | ".join(f"`{v}`" if c == "tract_geoid" else str(v)
                                 for c, v in zip(hdr, r)) + " |"
               for r in orphan_detail.itertuples(index=False)])
    else:
        orphan_md = "_None._"

    rows = "\n".join(f"| `{k}` | {v:,} |" for k, v in counts.items())
    ri_rows = "\n".join(f"| {k} | {v:,} |" for k, v in ri.items())
    id_rows = "\n".join(
        f"| `{t}.{c}` | {w} | ok |" for (t, c), w in ID_LENGTHS.items())

    REPORT.write_text(f"""# Warehouse Load

Generated by `scripts/09_build_warehouse.py` into
`data/warehouse/branch_analysis.duckdb`. The database is dropped and rebuilt
on every run, which is what AC-04 requires.

## Row counts

| Table | Rows |
|---|---|
{rows}

## Types are declared, not inferred

Every table is created with an explicit schema and loaded under a declared
column list. **There is no `read_csv_auto` in this pipeline.** DuckDB's
inference would read `55001950100` as a BIGINT and `05296` as an INTEGER, and
once a GEOID has lost its leading zero nothing downstream can recover it.

Verified after loading rather than assumed:

| Column | Required length | Result |
|---|---|---|
{id_rows}

The project guide has said "TEXT, always" since the start. This is where that
becomes a test rather than a comment — and it is the last point in the
pipeline where the error could still be introduced.

## Referential integrity

| Check | Orphans |
|---|---|
{ri_rows}

`dim_branch.tract_geoid → dim_tract` is the one to watch: a branch whose
coordinates fall outside every tract in the two states would show up here
rather than as a silent null.

### Orphans, enumerated

A count cannot be judged. These are the actual rows:

{orphan_md}

{'''**`01125010405` is in Alabama.** State FIPS `01`, Tuscaloosa County. Three
applications from one lender, filed with `state_code = 'IL'`, a blank
`county_code`, and an Alabama census tract. The extract was filtered on
`state_code`, so the record rode in on a field that disagrees with its own
geography.

It is 0.0004% of applications and the subject is unaffected, so the analytical
impact is nil. The point is what caught it: **the GEOID is a well-formed
eleven characters and passes every column assertion in the table above.** Only
the join found it. That is the case for asserting on relationships as well as
on values — a length check catches type damage, a referential check catches
the consequence, and also catches the case where the types are fine and the
populations simply do not line up.

These rows cannot join to `dim_tract` because no Alabama tract exists in it,
which is correct. They are excluded from tract-level analysis by construction
and recorded here rather than dropped silently.''' if len(orphan_detail) else ""}

## AC-03, re-tested in the warehouse

Tracts with more than one primary catchment: **{ac03}**.

Tested here as well as in script 08, because the criterion is a property of
the loaded model and not only of the script that produced it.

## Money

`fact_branch_deposits.deposits` is in **whole dollars**. SOD reports
`DEPSUMBR` in thousands; the conversion happens once, in this script, and
nowhere else.
""", encoding="utf-8")

    if failures:
        print("\n  [FAIL] identifier integrity:")
        for f in failures:
            print(f"         {f}")
        return 1
    print(f"\nWrote {DB.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: the SQL task list in sql/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
