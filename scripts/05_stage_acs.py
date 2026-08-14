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
STATE_FIPS = {"WI": "55", "IL": "17"}
LMI_THRESHOLD = 80.0          # FFIEC: tract income ratio below 80% of area

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

    cols = ["tract_geoid", "county_fips", "county_name", "cbsa", "cbsa_title",
            "tier", "population", "households", "median_hh_income",
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
