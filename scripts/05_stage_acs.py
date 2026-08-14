"""
05_stage_acs.py — stage tract attributes into dim_tract.

Writes data/staging/dim_tract.csv and docs/acs_suppression.md.

MISSINGNESS IS THREE THINGS, NOT ONE
------------------------------------
A tract with a suppressed median income, a tract with no households, and a
water polygon are three different facts that all arrive as "no value". Folding
them together loses the reason, and the reason is what determines the handling:

  suppressed        Census withheld the estimate because the sample was too
                    small. A real, inhabited tract whose value we do not know.
                    Excluded from averages; counted in denominators where the
                    denominator is tracts-that-exist.
  unpopulated       Zero households. Nothing was withheld; there is nothing to
                    report. Excluded from both averages and rate denominators.
  water_or_special  Census tract codes 99xx (water) and 98xx (special land
                    use). Not a residential geography at all. Excluded
                    entirely, and never counted as a coverage gap.

The AC-06 exception list already identified 22 water and unpopulated tracts.
This script must not quietly re-admit them as a third flavour of missing.

SENTINELS
---------
Census encodes suppression as large negative integers - -666666666 is the
common one, but -999999999, -888888888 and others encode different reasons,
and the set is not fixed. Filtering on the known value would silently admit
the others as real negative incomes.

So: filter on SIGN, and log the distinct sentinel values actually encountered.
Filtering on sign is safe; discarding which sentinel appeared would throw away
why the value is missing, and that is exactly the information worth keeping.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import geopandas
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "acs_suppression.md"
OUT = STAGING / "dim_tract.csv"

ACS_YEAR, TIGER_YEAR = 2024, 2024
ACS_GROWTH_YEAR = 2019        # 2015-2019, the NON-OVERLAPPING earlier vintage
GROWTH_SPAN_YEARS = 5         # midpoints 2017 -> 2022
STATE_FIPS = {"WI": "55", "IL": "17"}
LMI_THRESHOLD = 80.0          # FFIEC: tract income ratio below 80% of area

# A 2020 tract's 2019 household count is EXACT only where every contributing
# 2010 parent lies entirely inside it. Any parent shared with another 2020
# tract means the count is a uniform-density estimate.
#
# THE TEST IS FROM THE CHILD'S SIDE, WHICH IS NOT THE SAME QUESTION AS FROM
# THE PARENT'S. A 2020 tract taking 2% of one 2010 parent looks like an
# irrelevant sliver from the parent's perspective and is in fact the MOST
# estimated case from the child's: its entire household count is 2% of
# somebody else's, assumed uniform. Testing the parent's side marked exactly
# those tracts "direct" and produced a Kendall County tract growing 117% a
# year off an apportioned base of 34 households.
WHOLE_PARENT_THRESHOLD = 0.95     # w at or above this: parent effectively whole
ESTIMATED_SHARE_TOLERANCE = 0.01  # estimated share of households above which
                                  # the value is labelled apportioned

# A link below this share of its 2010 parent moves less than 1% of that
# tract's households and cannot move a growth rate materially. It is used for
# APPORTIONMENT, where it belongs, but NOT for deciding which areas are
# connected - see the cluster build below. The two uses need different
# treatment and conflating them produced clusters of 212 tracts.
MATERIAL_LINK_SHARE = 0.01

NUMERIC = {
    "B01003_001E": "population",
    "B19013_001E": "median_hh_income",
    "B19113_001E": "median_family_income",
    "B25003_001E": "households",
    "B25003_002E": "owner_occupied_units",
    "B25077_001E": "median_home_value",
}


def load_acs(name: str) -> pd.DataFrame:
    d = json.loads((RAW / name).read_text(encoding="utf-8"))
    return pd.DataFrame(d[1:], columns=d[0])


def clean_sentinels(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Coerce ACS numerics, treating ANY negative value as missing.

    Returns the frame and a per-column Counter of the sentinel values seen, so
    the report can say which suppression codes actually occurred rather than
    asserting the one everybody quotes.
    """
    seen: dict[str, Counter] = {}
    for src, dest in NUMERIC.items():
        v = pd.to_numeric(df[src], errors="coerce")
        neg = v[v < 0]
        if len(neg):
            seen[dest] = Counter(neg.astype("int64").tolist())
        df[dest] = v.where(v >= 0)
    return df, seen


def apportion_2019_households() -> pd.DataFrame:
    """Carry 2015-2019 household counts onto 2020 tract boundaries.

    THE TWO VINTAGES DO NOT SHARE A KEY. 2015-2019 is published on 2010 tract
    definitions and 2020-2024 on 2020 definitions. Census publishes the
    relationship file that bridges them; without it the join silently loses
    every tract that split or merged, and a tract that fails to join reads as
    a tract with no growth - a default standing in for a state.

    Households are apportioned by LAND AREA share, which assumes households
    are spread uniformly across a 2010 tract. That assumption is weakest
    exactly where the component carries the most signal, because tracts split
    BECAUSE they grew: WI+IL went from 4,533 tracts to 4,807. Hence
    growth_basis, so a consumer can see which values are estimates.

    Returns tract_geoid (2020), households_2019, growth_basis.
    """
    rel = pd.concat([
        pd.read_csv(RAW / f"tract_rel_2020_2010_{f}.txt", sep="|",
                    dtype={"GEOID_TRACT_20": "string", "GEOID_TRACT_10": "string"},
                    encoding="utf-8-sig")
        for f in STATE_FIPS.values()
    ], ignore_index=True)

    # Water-only overlaps carry no households and would inflate the split
    # counts. Dropped on land area, not on a name.
    rel["AREALAND_PART"] = pd.to_numeric(rel["AREALAND_PART"],
                                         errors="coerce").fillna(0)
    land = rel[rel["AREALAND_PART"] > 0].copy()

    # Share of each 2010 tract's land landing in this 2020 tract.
    land["w"] = (land["AREALAND_PART"]
                 / land.groupby("GEOID_TRACT_10")["AREALAND_PART"]
                       .transform("sum"))

    old = pd.concat([load_acs(f"acs5_{ACS_GROWTH_YEAR}_tract_{s.lower()}.json")
                     for s in STATE_FIPS], ignore_index=True)
    old["tract_geoid_2010"] = old["state"] + old["county"] + old["tract"]
    hh = pd.to_numeric(old["B25003_001E"], errors="coerce")
    # Same sign rule as clean_sentinels: ANY negative is a suppression code.
    old["hh_2019"] = hh.where(hh >= 0)
    parent_hh = old.set_index("tract_geoid_2010")["hh_2019"]

    land["parent_hh"] = land["GEOID_TRACT_10"].map(parent_hh)
    land["contrib"] = land["parent_hh"] * land["w"]

    # A 2020 tract whose parents are ALL suppressed has no basis at all. One
    # whose parents are partly suppressed has an understated basis, which
    # would read as spurious growth - both are refused below.
    # Households arriving from a parent this tract does NOT wholly contain.
    land["contrib_estimated"] = land["contrib"].where(
        land["w"] < WHOLE_PARENT_THRESHOLD, 0)

    agg = land.groupby("GEOID_TRACT_20").agg(
        households_2019=("contrib", "sum"),
        households_estimated=("contrib_estimated", "sum"),
        parents=("GEOID_TRACT_10", "nunique"),
        parents_missing=("parent_hh", lambda s: int(s.isna().sum())),
        max_w=("w", "max"),
    )
    agg["estimated_share"] = (agg["households_estimated"]
                              / agg["households_2019"].replace(0, pd.NA))

    agg["growth_basis"] = "direct"
    agg.loc[agg["estimated_share"] > ESTIMATED_SHARE_TOLERANCE,
            "growth_basis"] = "apportioned"
    # A tract assembled entirely from fragments of parents it does not
    # contain. The uniform-density assumption is not a rounding detail here,
    # it IS the number, so it gets its own state rather than hiding inside
    # "apportioned".
    agg.loc[agg["max_w"] < WHOLE_PARENT_THRESHOLD, "growth_basis"] = "fragmentary"
    # Refuse rather than estimate. An understated 2019 base manufactures
    # growth, and this component is 20% of the opportunity index.
    agg.loc[agg["parents_missing"] > 0, "growth_basis"] = "parent_suppressed"
    agg.loc[agg["parents_missing"] > 0, "households_2019"] = pd.NA

    # --- the exact geography, for tracts where the tract is not one -------
    #
    # A fragmentary tract's own 2019 count is uniform-density guesswork and it
    # shows: those values are six times more dispersed than direct ones and
    # produce a Kendall County tract at +117% a year. Nulling them would be
    # honest but expensive, because tracts fragment BECAUSE they grew - it
    # would delete growth signal from exactly the siting candidates BQ-5 is
    # looking for.
    #
    # There is a geography on which both vintages ARE exactly comparable: the
    # connected component of the 2020<->2010 overlap graph. Every 2010 parent
    # in a component lies wholly inside it and so does every 2020 child, so
    # summing either side is EXACT - no density assumption survives. The
    # growth of that area is measured, not estimated; what is assumed is only
    # that the children within it grew alike, which is a far weaker claim than
    # assuming 2010 households were spread evenly over land.
    # ONLY MATERIAL LINKS CONNECT. Census relationship files carry boundary-
    # resolution slivers: 345 of these rows cross a county line with a median
    # land area of 3,986 m2 and a median weight of 0.000028. They transfer no
    # households, but a graph built over them CHAINS otherwise separate areas
    # - one component reached 212 tracts spanning four counties and two
    # states, which is geometrically impossible and was the tell. Slivers stay
    # in the apportionment sum above, where their contribution is correctly
    # about zero; they are excluded here, where their effect is not small.
    links = land[land["w"] >= MATERIAL_LINK_SHARE]
    dropped = len(land) - len(links)

    parent_of = {}                      # 2010 -> component id
    child_of = {}                       # 2020 -> component id
    adj: dict[str, list[str]] = {}
    for c, p in zip(links["GEOID_TRACT_20"], links["GEOID_TRACT_10"]):
        adj.setdefault("c" + c, []).append("p" + p)
        adj.setdefault("p" + p, []).append("c" + c)
    seen, comp_id = set(), 0
    for start in adj:
        if start in seen:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:                    # iterative: recursion would blow up
            n = stack.pop()
            members.append(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        for m in members:
            (child_of if m[0] == "c" else parent_of)[m[1:]] = comp_id
        comp_id += 1

    sizes = pd.Series(list(child_of.values())).value_counts()
    print(f"growth clusters: {len(sizes):,} built from {len(links):,} material "
          f"links ({dropped:,} slivers excluded from the graph); "
          f"largest {sizes.max()} tracts, median {sizes.median():.0f}")

    agg = agg.reset_index().rename(columns={"GEOID_TRACT_20": "tract_geoid"})
    agg["growth_cluster"] = agg["tract_geoid"].map(child_of)
    old["growth_cluster"] = old["tract_geoid_2010"].map(parent_of)
    cluster_2019 = old.groupby("growth_cluster")["hh_2019"].sum(min_count=1)
    agg["cluster_households_2019"] = agg["growth_cluster"].map(cluster_2019)
    agg["cluster_tracts"] = agg["growth_cluster"].map(
        agg["growth_cluster"].value_counts())

    return agg[["tract_geoid", "households_2019", "growth_basis",
                "growth_cluster", "cluster_households_2019", "cluster_tracts"]]


def classify(row) -> str:
    """suppressed | unpopulated | water_or_special | ok"""
    tract_code = row["tract_geoid"][5:]
    if tract_code.startswith(("99", "98")):
        return "water_or_special"
    if pd.notna(row["households"]) and row["households"] == 0:
        return "unpopulated"
    if pd.isna(row["households"]) or pd.isna(row["median_family_income"]):
        return "suppressed"
    return "ok"


def main() -> int:
    frames = [load_acs(f"acs5_{ACS_YEAR}_tract_{s.lower()}.json") for s in STATE_FIPS]
    tracts = pd.concat(frames, ignore_index=True)
    tracts["tract_geoid"] = tracts["state"] + tracts["county"] + tracts["tract"]
    tracts["county_fips"] = tracts["state"] + tracts["county"]
    tracts, sentinels = clean_sentinels(tracts)
    print(f"ACS tracts loaded: {len(tracts):,}")

    # --- county -> CBSA, and the three-way tier -------------------------
    import openpyxl
    wb = openpyxl.load_workbook(RAW / "cbsa_delineation_2023.xlsx", read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows)
              if r and "CBSA Code" in [str(c) for c in r])
    hdr = [str(c) for c in rows[hi]]
    dl = pd.DataFrame([dict(zip(hdr, r)) for r in rows[hi + 1:] if r and r[0]])
    dl["county_fips"] = (dl["FIPS State Code"].astype(str).str.zfill(2)
                         + dl["FIPS County Code"].astype(str).str.zfill(3))
    dl = dl.drop_duplicates("county_fips").set_index("county_fips")
    tracts["cbsa"] = tracts["county_fips"].map(dl["CBSA Code"])
    tracts["cbsa_title"] = tracts["county_fips"].map(dl["CBSA Title"])
    cbsa_type = tracts["county_fips"].map(
        dl["Metropolitan/Micropolitan Statistical Area"])
    tracts["tier"] = cbsa_type.map({
        "Metropolitan Statistical Area": "metro",
        "Micropolitan Statistical Area": "micro"}).fillna("rural")

    # --- LMI: FFIEC ratio primary, ACS fallback (assumption A-03) --------
    lar = pd.read_csv(RAW / f"hmda_tract_subset_2025_wi_il.csv.gz",
                      usecols=["census_tract", "tract_to_msa_income_percentage"],
                      dtype={"census_tract": "string"})
    ratio = (lar.dropna(subset=["census_tract"])
                .assign(r=lambda d: pd.to_numeric(
                    d["tract_to_msa_income_percentage"], errors="coerce"))
                .groupby("census_tract")["r"].median())
    tracts["tract_to_area_income_pct"] = tracts["tract_geoid"].map(ratio)
    tracts["lmi_basis"] = tracts["tract_to_area_income_pct"].notna().map(
        {True: "ffiec_ratio", False: ""})

    # Fallback for tracts with no lending activity: ACS tract family income
    # over CBSA family income. A different basis, so it is labelled as one.
    cb = load_acs(f"acs5_{ACS_YEAR}_cbsa.json")
    cb["area_family_income"] = pd.to_numeric(cb["B19113_001E"], errors="coerce")
    cb.loc[cb["area_family_income"] < 0, "area_family_income"] = pd.NA
    cbsa_col = [c for c in cb.columns if "metropolitan" in c][0]
    area = cb.set_index(cbsa_col)["area_family_income"]
    fallback = (tracts["median_family_income"]
                / tracts["cbsa"].map(area) * 100)
    need = tracts["tract_to_area_income_pct"].isna() & fallback.notna()
    tracts.loc[need, "tract_to_area_income_pct"] = fallback[need]
    tracts.loc[need, "lmi_basis"] = "acs_fallback"

    # Nullable boolean, not bool. A tract with no income basis is neither LMI
    # nor non-LMI, and pandas 3 correctly refuses to upcast a bool column to
    # hold that. Collapsing unknown into False would inflate the non-LMI count
    # and quietly improve the BQ-6 equity result.
    _ratio = tracts["tract_to_area_income_pct"]
    tracts["lmi_flag"] = ((_ratio < LMI_THRESHOLD)
                          .astype("boolean").mask(_ratio.isna()))

    # --- centroids ------------------------------------------------------
    geo = pd.concat([geopandas.read_file(
        f"zip://{RAW / f'tl_{TIGER_YEAR}_{f}_tract.zip'}") for f in STATE_FIPS.values()])
    cent = geo.to_crs("EPSG:5070").geometry.centroid.to_crs("EPSG:4326")
    geo = geo.assign(centroid_lat=cent.y.values, centroid_lon=cent.x.values)
    g = geo.set_index("GEOID")
    tracts["centroid_lat"] = tracts["tract_geoid"].map(g["centroid_lat"])
    tracts["centroid_lon"] = tracts["tract_geoid"].map(g["centroid_lon"])
    tracts["county_name"] = tracts["NAME"].str.split(";").str[-2].str.strip()

    tracts["tract_status"] = tracts.apply(classify, axis=1)

    # --- household growth, 2015-2019 -> 2020-2024 -----------------------
    g19 = apportion_2019_households()
    tracts = tracts.merge(g19, on="tract_geoid", how="left")
    # A tract with no 2010 counterpart at all. Named, not left as a silent NaN.
    tracts["growth_basis"] = tracts["growth_basis"].fillna("no_2010_counterpart")

    # CAGR over the gap between vintage midpoints (2017 -> 2022), so the
    # component is comparable with deposit_market_growth rather than being a
    # raw percentage over an unstated period.
    #
    # NULL IS WRITTEN BEFORE THE ARITHMETIC, not left to fall out of it. A
    # zero or missing 2019 base must not become an infinite or fabricated
    # growth rate - the same three-valued-logic trap that reported a branch
    # with no CAGR as having a positive trajectory.
    # A tract with no households in 2024 has no growth rate - it has an
    # absence. Water and unpopulated tracts otherwise return exactly -100%,
    # which is a category presenting itself as a measurement and would sort
    # to the bottom of any growth ranking as though it were a finding.
    structural = tracts["tract_status"].isin(["water_or_special", "unpopulated"])
    tracts.loc[structural, "growth_basis"] = "not_applicable"

    h19, h24 = tracts["households_2019"], tracts["households"]
    computable = h19.notna() & h24.notna() & (h19 > 0) & ~structural
    tracts["household_growth_pct"] = pd.NA
    tracts.loc[computable, "household_growth_pct"] = (
        ((h24[computable] / h19[computable]) ** (1 / GROWTH_SPAN_YEARS) - 1) * 100
    ).round(4)
    tracts.loc[~computable & tracts["growth_basis"].isin(
        ["direct", "apportioned", "fragmentary"]), "growth_basis"] = "no_2019_base"

    # For fragmentary tracts, replace the artifact with the measurement: the
    # cluster's growth, which needs no density assumption. Both series are
    # kept - household_growth_pct_tract preserves what the tract-level
    # arithmetic said, so the correction stays auditable rather than being
    # silently substituted.
    tracts["household_growth_pct_tract"] = tracts["household_growth_pct"]
    cl24 = tracts.groupby("growth_cluster")["households"].transform("sum")
    cl19 = tracts["cluster_households_2019"]
    ok = (tracts["growth_basis"] == "fragmentary") & cl19.notna() & (cl19 > 0) \
        & cl24.notna() & (cl24 > 0)
    tracts.loc[ok, "household_growth_pct"] = (
        ((cl24[ok] / cl19[ok]) ** (1 / GROWTH_SPAN_YEARS) - 1) * 100).round(4)
    tracts.loc[ok, "growth_basis"] = "cluster"
    # Fragmentary with no usable cluster either: refuse, do not fall back.
    tracts.loc[(tracts["growth_basis"] == "fragmentary"),
               "household_growth_pct"] = pd.NA
    tracts.loc[(tracts["growth_basis"] == "fragmentary"),
               "growth_basis"] = "fragmentary_no_cluster"

    cl = tracts.loc[tracts["growth_basis"] == "cluster", "cluster_tracts"]
    if len(cl):
        print(f"\ncluster basis covers {len(cl):,} tracts; cluster size "
              f"median {cl.median():.0f}, p95 {cl.quantile(0.95):.0f}, "
              f"max {cl.max():.0f} tracts")

    gb = tracts["growth_basis"].value_counts()
    print("\nhousehold_growth basis:")
    for k, v in gb.items():
        print(f"   {k:22s} {v:>6,}  {v / len(tracts):>6.2%}")
    print(f"   computable growth rates: "
          f"{tracts['household_growth_pct'].notna().sum():,}")

    cols = ["tract_geoid", "county_fips", "county_name", "cbsa", "cbsa_title",
            "tier", "population", "households", "households_2019",
            "household_growth_pct", "household_growth_pct_tract",
            "growth_basis", "growth_cluster", "cluster_tracts",
            "median_hh_income",
            "median_family_income", "median_home_value", "owner_occupied_units",
            "tract_to_area_income_pct", "lmi_flag", "lmi_basis",
            "tract_status", "centroid_lat", "centroid_lon"]
    dim = tracts[cols].sort_values("tract_geoid")
    STAGING.mkdir(parents=True, exist_ok=True)
    dim.to_csv(OUT, index=False)

    status = dim["tract_status"].value_counts()
    by_tier = pd.crosstab(dim["tier"], dim["tract_status"])
    by_tier_pct = (by_tier.div(by_tier.sum(axis=1), axis=0) * 100).round(1)

    def md(df, fmt="{}"):
        head = [df.index.name or ""] + [str(c) for c in df.columns]
        out = ["| " + " | ".join(head) + " |",
               "|" + "|".join("---" for _ in head) + "|"]
        for i, r in df.iterrows():
            out.append("| " + " | ".join([str(i)]
                       + [fmt.format(v) for v in r]) + " |")
        return "\n".join(out)

    sent_rows = []
    for col, ctr in sorted(sentinels.items()):
        for val, n in sorted(ctr.items()):
            sent_rows.append(f"| `{col}` | `{val}` | {n:,} |")

    supp_tier = by_tier_pct.get("suppressed", pd.Series(dtype=float))
    worst = supp_tier.idxmax() if len(supp_tier) else "n/a"

    REPORT.write_text(f"""# ACS Suppression and Tract Status

Generated by `scripts/05_stage_acs.py`. {len(dim):,} tracts in WI+IL.

## Missingness is three things

A suppressed estimate, an unpopulated tract, and a water polygon all arrive as
"no value" and are not the same fact. Each gets its own status and its own
handling rule.

{md(status.to_frame("tracts"), "{:,}")}

| Status | Meaning | Handling |
|---|---|---|
| `ok` | Values present | Used normally |
| `suppressed` | Census withheld the estimate — sample too small. A real, inhabited tract whose value is unknown | Excluded from averages. **Counted** in denominators that mean "tracts that exist" |
| `unpopulated` | Zero households. Nothing withheld; nothing to report | Excluded from averages **and** from rate denominators |
| `water_or_special` | Tract codes `99xx` (water) and `98xx` (special land use). Not a residential geography | Excluded entirely. **Never counted as a coverage gap** — these are the tracts already enumerated on the AC-06 exception list, and they must not be re-admitted here as a third flavour of missing |

## Sentinel values actually encountered

Census encodes suppression as large negative integers. `-666666666` is the
one usually quoted, but it is not the only one and the set is not fixed. This
pipeline filters on **sign**, not on a known value — then records which codes
appeared, because the code carries the reason and the reason is worth keeping.

| Field | Sentinel | Tracts |
|---|---|---|
{chr(10).join(sent_rows) if sent_rows else "| _none encountered_ | | |"}

## Suppression by catchment tier — a coverage finding, not a log line

Census suppresses on small sample size, which is not distributed evenly:
low-population tracts are disproportionately rural. That matters here because
the non-CBSA tier draws a **20.8-mile** radius and pulls in the most tracts
per branch, so a thinner data base in that tier would quietly weaken exactly
the comparisons that reach furthest.

Share of tracts in each status, by tier (%):

{md(by_tier_pct, "{:.1f}")}

Counts:

{md(by_tier, "{:,}")}

**Highest suppression rate: `{worst}` tier.** Any tier-level comparison — and
any statement that one tier's catchments perform differently from another's —
must account for this before it is made.

## LMI basis

`lmi_flag` is `tract_to_area_income_pct < {LMI_THRESHOLD:.0f}`, per assumption
A-03. The basis is recorded per tract because two different sources are in
play and mixing them silently would be indefensible.

{md(dim["lmi_basis"].replace("", "none").value_counts().to_frame("tracts"), "{:,}")}

- `ffiec_ratio` — the FFIEC's own published tract-to-area income ratio, taken
  from the loan-level file. The figure examiners use.
- `acs_fallback` — ACS tract median family income over CBSA median family
  income, for tracts with no lending activity. A reconstruction, not the
  official figure, and labelled as such.
- `none` — no basis available by either route. These are the AC-06 exceptions.
""", encoding="utf-8")

    print(f"\nTract status:\n{status.to_string()}")
    print(f"\nBy tier (%):\n{by_tier_pct.to_string()}")
    print(f"\nSentinel values seen: "
          f"{ {k: dict(v) for k, v in sentinels.items()} }")
    print(f"\nLMI basis:\n{dim['lmi_basis'].replace('', 'none').value_counts().to_string()}")
    print(f"\nWrote {OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: python scripts/06_stage_hmda.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
