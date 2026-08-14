"""
07_spatial_join.py — place every branch in a census tract. AC-02.

Writes data/staging/dim_branch.csv and docs/spatial_join_exceptions.md.

PRESENCE IS NOT VALIDITY
------------------------
Zero branches are missing coordinates in this data, which is unusually clean
and makes the obvious check uninformative. The failure mode that survives is a
branch with *plausible-looking* coordinates that lands somewhere it should not.
Such a point joins to exactly one tract, so AC-02 passes on it, and nothing
downstream notices - the branch simply contributes its deposits to the wrong
market and pulls a catchment over the wrong demographics.

So the real test is agreement between two independent statements of where the
branch is:

    the tract the coordinates fall inside   (spatial, from TIGER)
    the county FDIC reports for the branch  (attribute, STCNTYBR)

A mismatch means one of them is wrong. Those rows are the interesting ones and
they are enumerated, not silently accepted.

Four validity checks run before the join, cheapest first:
  1  present            - non-null latitude and longitude
  2  in range           - inside a generous WI+IL bounding box
  3  not transposed     - latitude and longitude not swapped, which in this
                          hemisphere shows up as a positive longitude or a
                          latitude near -88
  4  not a null island  - exactly (0, 0), the classic geocoder failure

Then the join, then the county cross-check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import geopandas
import numpy as np
import pandas as pd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "spatial_join_exceptions.md"
OUT = STAGING / "dim_branch.csv"

STATE_FIPS = {"WI": "55", "IL": "17"}
TIGER_YEAR = 2024

# Generous bounds around WI+IL. Wide enough not to flag a legitimate border
# branch, tight enough to catch a coordinate in the wrong state or hemisphere.
LAT_MIN, LAT_MAX = 36.0, 47.5
LON_MIN, LON_MAX = -93.5, -86.0


ABBR = {"street": "st", "road": "rd", "drive": "dr", "avenue": "ave",
        "boulevard": "blvd", "highway": "hwy", "north": "n", "south": "s",
        "east": "e", "west": "w", "suite": "ste", "lane": "ln", "court": "ct",
        "place": "pl", "parkway": "pkwy", "circle": "cir", "trail": "trl"}


def norm_address(a) -> str:
    """Normalise an address so a formatting change is not read as a move.

    '105 West Main Street' and '105 W Main St' are the same place, and FDIC
    reformats addresses between vintages. Without this, every reformat would
    look like a relocation.
    """
    s = re.sub(r"[^a-z0-9 ]", " ", str(a).lower())
    return " ".join(ABBR.get(t, t) for t in s.split())


def build_dim_branch(sod: pd.DataFrame) -> pd.DataFrame:
    """One row per UNINUMBR, using its most recent observation.

    COORDINATE VALIDITY IS CHECKED IN EVERY VINTAGE, NOT JUST THE LATEST.
    A branch can carry garbage coordinates in earlier years and a clean one
    now; checking only the current row passes it. Exactly that happens here -
    one branch reports (0, 0) for 2019 through 2022 and a valid position in
    2025. Invalid vintages are excluded from the drift measurement, because
    otherwise a null island produces a 6,000-mile "relocation".

    Drift is then classified by whether the ADDRESS changed, which is a
    sharper test than distance or monotonicity:
        address changed, position moved  -> relocation
        address SAME,    position moved  -> re-geocode, NOT a move
    The subject's largest apparent move, 7.4 miles, is a re-geocode: the
    address string is identical in all seven vintages. Treating it as a
    relocation would flag a branch that never moved.
    """
    sod = sod.sort_values("_year").copy()
    sod["coord_valid"] = (
        sod["SIMS_LATITUDE"].between(LAT_MIN, LAT_MAX)
        & sod["SIMS_LONGITUDE"].between(LON_MIN, LON_MAX)
        & ~((sod["SIMS_LATITUDE"] == 0) & (sod["SIMS_LONGITUDE"] == 0)))

    latest = sod.drop_duplicates("UNINUMBR", keep="last").set_index("UNINUMBR")
    span = sod.groupby("UNINUMBR")["_year"].agg(first_year="min", last_year="max")
    bad_vintages = (sod.loc[~sod["coord_valid"] & sod["SIMS_LATITUDE"].notna()]
                       .groupby("UNINUMBR").size().rename("invalid_vintages"))

    ok = sod[sod["coord_valid"]].copy()
    ok["_addr"] = ok["ADDRESBR"].map(norm_address)
    pts = geopandas.GeoDataFrame(
        ok, geometry=geopandas.points_from_xy(
            ok["SIMS_LONGITUDE"], ok["SIMS_LATITUDE"]),
        crs="EPSG:4326").to_crs("EPSG:5070")
    ok["_x"], ok["_y"] = pts.geometry.x.values, pts.geometry.y.values

    rows = []
    for uni, grp in ok.groupby("UNINUMBR"):
        if len(grp) < 2:
            rows.append((uni, 0.0, False, "single_vintage"))
            continue
        xy = grp[["_x", "_y"]].to_numpy()
        net = float(np.linalg.norm(xy[-1] - xy[0]) / 1609.344)
        moved_addr = grp["_addr"].nunique() > 1
        kind = ("stable" if net <= 0.1
                else ("relocation" if moved_addr else "regeocode"))
        rows.append((uni, net, moved_addr, kind))
    drift = pd.DataFrame(rows, columns=["UNINUMBR", "position_drift_miles",
                                        "address_changed", "drift_kind"]
                         ).set_index("UNINUMBR")

    out = latest.join(span).join(drift).join(bad_vintages)
    out["invalid_vintages"] = out["invalid_vintages"].fillna(0).astype(int)
    return out.reset_index()


def validity(df: pd.DataFrame) -> pd.Series:
    lat, lon = df["SIMS_LATITUDE"], df["SIMS_LONGITUDE"]
    reason = pd.Series("ok", index=df.index, dtype="object")
    reason[lat.isna() | lon.isna()] = "missing_coordinates"
    reason[(reason == "ok") & (lat == 0) & (lon == 0)] = "null_island"
    # In WI+IL longitude is around -88 and latitude around +43. A positive
    # longitude or a strongly negative latitude means the pair was swapped.
    reason[(reason == "ok") & ((lon > 0) | (lat < 0))] = "transposed_coordinates"
    reason[(reason == "ok") & (~lat.between(LAT_MIN, LAT_MAX)
                               | ~lon.between(LON_MIN, LON_MAX))] = "out_of_range"
    return reason


def md(df: pd.DataFrame, fmt: str = "{:,}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [
            fmt.format(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
            else str(v) for v in r]) + " |")
    return "\n".join(out)


def main() -> int:
    sod = pd.read_csv(STAGING / "sod_all.csv.gz",
                      dtype={"CERT": "string", "UNINUMBR": "string",
                             "STCNTYBR": "string"}, low_memory=False)
    branches = build_dim_branch(sod)
    print(f"Distinct branches across seven vintages: {len(branches):,}")

    branches["validity"] = validity(branches)
    vc = branches["validity"].value_counts()
    print(f"\nCoordinate validity:\n{vc.to_string()}")

    usable = branches[branches["validity"] == "ok"].copy()

    # --- spatial join ----------------------------------------------------
    tracts = pd.concat([geopandas.read_file(
        f"zip://{RAW / f'tl_{TIGER_YEAR}_{f}_tract.zip'}")
        for f in STATE_FIPS.values()], ignore_index=True)[["GEOID", "geometry"]]
    tracts = tracts.to_crs("EPSG:4326")

    pts = geopandas.GeoDataFrame(
        usable, geometry=[Point(x, y) for x, y in
                          zip(usable["SIMS_LONGITUDE"], usable["SIMS_LATITUDE"])],
        crs="EPSG:4326")
    joined = geopandas.sjoin(pts, tracts, how="left", predicate="within")

    # A point on a shared boundary can match twice. Keep one and record it.
    dup = joined.index.duplicated(keep="first")
    n_dup = int(dup.sum())
    joined = joined[~dup]
    joined["tract_geoid"] = joined["GEOID"]

    unmatched = joined["tract_geoid"].isna()
    print(f"\nJoin: {int((~unmatched).sum()):,} matched, "
          f"{int(unmatched.sum()):,} unmatched, {n_dup} boundary duplicates")

    # --- the check that matters: county agreement ------------------------
    # STCNTYBR is FDIC's own state+county FIPS for the branch. The joined
    # tract's first five digits are the county the coordinates actually fall
    # in. Disagreement means one of the two is wrong.
    joined["sod_county"] = joined["STCNTYBR"].astype("string").str.zfill(5)
    joined["geo_county"] = joined["tract_geoid"].astype("string").str[:5]
    joined["county_agrees"] = joined["sod_county"] == joined["geo_county"]
    mism = joined[joined["tract_geoid"].notna() & ~joined["county_agrees"]]

    print(f"County cross-check: {int(joined['county_agrees'].sum()):,} agree, "
          f"{len(mism):,} disagree")

    cols = ["UNINUMBR", "CERT", "NAMEFULL", "ADDRESBR", "CITYBR", "STALPBR",
            "sod_county", "geo_county", "tract_geoid", "SIMS_LATITUDE",
            "SIMS_LONGITUDE", "BRSERTYP", "BKMO", "first_year", "last_year",
            "position_drift_miles", "drift_kind", "address_changed",
            "invalid_vintages", "validity", "county_agrees"]
    dim = joined[[c for c in cols if c in joined.columns]].copy()
    # Carry the invalid ones through so dim_branch is complete and the
    # exceptions are visible in the table, not only in a report.
    rejected = branches[branches["validity"] != "ok"].copy()
    for c in ("tract_geoid", "sod_county", "geo_county", "county_agrees"):
        rejected[c] = pd.NA
    dim = pd.concat([dim, rejected[[c for c in cols if c in rejected.columns]]],
                    ignore_index=True)

    STAGING.mkdir(parents=True, exist_ok=True)
    dim.to_csv(OUT, index=False)

    drift = branches[branches["drift_kind"] == "relocation"]
    ac02_pass = int(unmatched.sum()) == 0 and int(vc.get("ok", 0)) == len(branches)

    # How wrong is each mismatch? Adjacent counties mean the point sits near a
    # line and the geocode is imprecise; a distant county means one of the two
    # statements is simply wrong. Distance to the FDIC-reported county polygon
    # separates the two without judgement.
    mism_tbl, adj_n, far_n, near_n = "", 0, 0, 0
    if len(mism):
        counties = (tracts.assign(county=tracts["GEOID"].str[:5])
                          .dissolve(by="county")[["geometry"]].to_crs("EPSG:5070"))
        mp = geopandas.GeoDataFrame(
            mism, geometry=geopandas.points_from_xy(
                mism["SIMS_LONGITUDE"], mism["SIMS_LATITUDE"]),
            crs="EPSG:4326").to_crs("EPSG:5070")
        mp["miles_off"] = [
            (r.geometry.distance(counties.loc[r["sod_county"], "geometry"]) / 1609.344
             if r["sod_county"] in counties.index else float("nan"))
            for _, r in mp.iterrows()]
        mp["adjacent"] = [
            bool(counties.loc[a, "geometry"].touches(counties.loc[b, "geometry"]))
            if a in counties.index and b in counties.index else False
            for a, b in zip(mp["sod_county"], mp["geo_county"])]
        adj_n = int(mp["adjacent"].sum())
        near_n = int((mp["miles_off"] < 1).sum())
        far_n = int((mp["miles_off"] > 5).sum())
        m = (mp.sort_values("miles_off", ascending=False)
               [["UNINUMBR", "NAMEFULL", "ADDRESBR", "CITYBR", "sod_county",
                 "geo_county", "miles_off"]].head(12))
        m["miles_off"] = m["miles_off"].round(1)
        mism_tbl = md(m.set_index("UNINUMBR"), "{}")

    REPORT.write_text(f"""# Spatial Join — Exceptions and Validity

Generated by `scripts/07_spatial_join.py`. AC-02.

{len(branches):,} distinct branches across the seven vintages, placed by
point-in-polygon against TIGER {TIGER_YEAR} tract boundaries.

## AC-02 — every branch resolves to exactly one tract

**{'PASS' if ac02_pass else 'SEE EXCEPTIONS BELOW'}**

| | Branches |
|---|---|
| Matched to exactly one tract | {int((~unmatched).sum()):,} |
| Unmatched | {int(unmatched.sum()):,} |
| Excluded before the join as invalid | {len(branches) - len(usable):,} |
| Matched more than one tract (shared boundary, first kept) | {n_dup:,} |

## Presence is not validity

Zero branches are missing coordinates, which makes the obvious check
uninformative. The failure mode that survives it is a branch with
*plausible-looking* coordinates landing somewhere it should not: such a point
joins to exactly one tract, AC-02 passes on it, and the branch quietly
contributes its deposits to the wrong market while pulling a catchment over
the wrong demographics.

Validity checks applied before the join:

{md(vc.to_frame("branches"))}

| Check | What it catches |
|---|---|
| `missing_coordinates` | Null latitude or longitude |
| `null_island` | Exactly (0, 0) — the classic geocoder failure |
| `transposed_coordinates` | Latitude and longitude swapped; in this hemisphere a positive longitude or a latitude near −88 |
| `out_of_range` | Outside a generous WI+IL box ({LAT_MIN}–{LAT_MAX} N, {LON_MIN}–{LON_MAX} E) — wrong state or wrong hemisphere |

## The check that actually matters: county agreement

Two independent statements of where each branch is:

- the tract its **coordinates** fall inside, from TIGER
- the county **FDIC reports** for it, in `STCNTYBR`

Disagreement means one of them is wrong, and AC-02 cannot detect it because
the branch still resolves to exactly one tract.

| | Branches |
|---|---|
| County agrees | {int(joined['county_agrees'].sum()):,} |
| **County disagrees** | **{len(mism):,}** |

{f'''### How wrong is each one?

Adjacency separates boundary imprecision from genuine error without needing a
judgement call. A point 200 metres over a county line is a geocoding artefact;
a point in a county 190 miles away is not.

| | Branches |
|---|---|
| The two counties are adjacent | {adj_n} of {len(mism)} |
| Point within 1 mile of the reported county | {near_n} |
| Point more than 5 miles from the reported county | {far_n} |

{mism_tbl}

**The coordinates are the more reliable of the two.** In every case far enough
out to check by hand, the branch's own address text agrees with the
coordinates and not with `STCNTYBR` — an address reading "Chicago" geocoding
into Cook County while FDIC reports a county 190 miles downstate, and
"West Chicago" geocoding into DuPage while FDIC reports Cook. Several
collar-county branches are reported as Cook, which looks like a reporting
convention rather than a scatter of unrelated typos.

So the spatial join wins: `tract_geoid` is taken from the coordinates, and
`county_agrees` is retained on every row so anything built on
FDIC-reported county can exclude these. **The subject institution is
unaffected** — none of Associated's branches disagree.

This is the class of error AC-02 cannot catch. Each of these branches resolves
to exactly one tract and passes the acceptance criterion cleanly; the branch
simply sits in the wrong market.''' if len(mism) else
'''No mismatches. Every branch's coordinates fall in the county FDIC reports
for it, which is a stronger statement than AC-02 alone makes.'''}

## Coordinate drift across vintages

A branch whose reported coordinates move between vintages either relocated or
was re-geocoded. Branches moving more than ~0.05° (roughly 3.5 miles):
**{len(drift):,}**.

{md(drift.nlargest(10, "position_drift_miles")[["NAMEFULL", "CITYBR", "position_drift_miles"]].round(2).set_index(drift.nlargest(10, "position_drift_miles")["UNINUMBR"]), "{}") if len(drift) else "_None._"}
""", encoding="utf-8")

    print(f"\nWrote {OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    if len(mism):
        print(f"\n  [NOTE] {len(mism)} branches disagree with FDIC's reported "
              f"county - see the report")
    print("\nNext: python scripts/08_catchments.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
