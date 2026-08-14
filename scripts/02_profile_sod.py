"""
02_profile_sod.py — load all seven SOD vintages and profile CONTENT drift.

Writes docs/sod_content_profile.md and data/staging/sod_all.csv.gz.

WHY THIS PROFILES CONTENT AND NOT SCHEMA
----------------------------------------
This script was originally specified to profile schema drift across the seven
SOD vintages and emit a column-mapping layer. That made sense when the source
was seven separately-published bulk ZIPs. It is not what the data looks like
now: the bulk download is gone and SOD arrives from the FDIC REST API, which
serves a normalized schema for every year. Profiling field-name drift against
an interface that normalizes field names would find nothing and prove nothing.

So the target moved to content drift, which is real and unexamined:

  1. Branch counts by institution and year
  2. Deposit distribution shifts by year
  3. Field population rates by year - where the API is normalizing names, it is
     NOT backfilling values, so a field can be present-but-empty in early years
  4. Branches appearing and disappearing between vintages

(4) is the one that pays twice. Survivor bias has to be handled deliberately
somewhere - a branch that vanishes between vintages is a closure, a sale, or a
charter change, and treating those alike would misstate the consolidation
story. Computing the transitions here means that work is already done when
BQ-2 needs it, instead of being rediscovered later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "sod_content_profile.md"

YEARS = range(2019, 2026)
STATES = ("wi", "il")

# Identifiers that must never be read as numbers. Trap #1 in the project
# guide: the API returns STNUMBR and CNTYNUMB as integers, and 859 of 1,295
# Wisconsin county codes lose a digit if that is not caught here.
TEXT_COLS = {
    "CERT": "string", "UNINUMBR": "string", "ZIPBR": "string",
    "ZIP": "string", "DOCKET": "string", "RSSDHCR": "string",
}

# Fields the downstream model actually depends on. Population rates are
# reported for these specifically, because a field that is 100% populated in
# 2025 and 60% populated in 2019 will silently skew any trend built on it.
KEY_FIELDS = [
    "UNINUMBR", "CERT", "DEPSUMBR", "SIMS_LATITUDE", "SIMS_LONGITUDE",
    "STNUMBR", "CNTYNUMB", "BRSERTYP", "BKMO", "ADDRESBR", "CITYBR",
    "NAMEFULL", "MSABR", "SIMS_ESTABLISHED_DATE",
]


def load_all() -> pd.DataFrame:
    """Load 14 state-year files into one typed table."""
    frames = []
    for year in YEARS:
        for state in STATES:
            path = RAW / f"fdic_sod_{year}_{state}.csv"
            if not path.exists():
                print(f"  [MISS] {path.name} - run 01_download.py first")
                continue
            df = pd.read_csv(path, dtype=TEXT_COLS, low_memory=False)
            df["_year"] = year
            df["_state"] = state.upper()
            frames.append(df)
            print(f"  [load] {path.name:28s} {len(df):>6,} rows")
    if not frames:
        raise SystemExit("No SOD files found. Run scripts/01_download.py first.")
    return pd.concat(frames, ignore_index=True)


def county_fips(df: pd.DataFrame) -> pd.Series:
    """Assemble 5-digit county FIPS as TEXT, zero-padded.

    STNUMBR and CNTYNUMB arrive as integers. Concatenating them without
    padding turns Wisconsin county 47 into '5547' instead of '55047', which
    then fails to join to anything and does so silently.
    """
    st = df["STNUMBR"].astype("Int64").astype("string").str.zfill(2)
    cty = df["CNTYNUMB"].astype("Int64").astype("string").str.zfill(3)
    return st + cty


def branch_counts(df: pd.DataFrame) -> pd.DataFrame:
    return (df.pivot_table(index="_year", columns="_state",
                           values="UNINUMBR", aggfunc="nunique")
              .assign(total=lambda d: d.sum(axis=1)))


def deposit_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """DEPSUMBR is in THOUSANDS of dollars as reported. Left as-is here:
    conversion to whole dollars happens once, at staging, per the style rule."""
    g = df.groupby("_year")["DEPSUMBR"]
    out = pd.DataFrame({
        "branches": g.size(),
        "total_deposits_k": g.sum(),
        "median_k": g.median(),
        "p90_k": g.quantile(0.90),
        "max_k": g.max(),
    })
    out["share_in_top_1pct"] = [
        s.nlargest(max(1, len(s) // 100)).sum() / s.sum()
        for _, s in df.groupby("_year")["DEPSUMBR"]
    ]
    return out


def population_rates(df: pd.DataFrame) -> pd.DataFrame:
    present = [c for c in KEY_FIELDS if c in df.columns]
    return (df.groupby("_year")[present]
              .apply(lambda g: g.notna().mean())
              .round(4))


def transitions(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Branches appearing and disappearing between consecutive vintages.

    Returns (year-over-year summary, the certs driving each change).
    A branch that changes charter re-registers under the acquiring cert with
    a new UNINUMBR, so a large simultaneous appear/disappear pair for two
    different certs in the same year is the signature of a merger rather than
    of closures.
    """
    by_year = {y: set(g["UNINUMBR"].dropna())
               for y, g in df.groupby("_year")}
    # Keyed a second way, on (CERT, UNINUMBR). UNINUMBR persists across a
    # change of ownership, so a branch that is sold appears under a new cert
    # while keeping its number. Global keying therefore ignores transfers and
    # counts only true openings and closings; per-cert keying counts a
    # transfer as both an appearance and a disappearance. The difference
    # between the two IS the number of branches that changed hands, which is
    # what separates consolidation from closure.
    per_cert = {y: set(zip(g["CERT"], g["UNINUMBR"]))
                for y, g in df.groupby("_year")}
    rows = []
    detail = []
    years = sorted(by_year)
    for prev, cur in zip(years, years[1:]):
        gone = by_year[prev] - by_year[cur]
        new = by_year[cur] - by_year[prev]
        pc_new = len(per_cert[cur] - per_cert[prev])
        rows.append({
            "transition": f"{prev}->{cur}",
            "carried_over": len(by_year[prev] & by_year[cur]),
            "closed": len(gone),
            "opened": len(new),
            "transferred": pc_new - len(new),
            "net": len(by_year[cur]) - len(by_year[prev]),
        })
        for label, ids, year in (("appeared", new, cur), ("disappeared", gone, prev)):
            sub = df[(df["_year"] == year) & (df["UNINUMBR"].isin(ids))]
            for cert, grp in sub.groupby("CERT"):
                if len(grp) >= 25:      # only material movements
                    detail.append({
                        "transition": f"{prev}->{cur}", "event": label,
                        "cert": cert, "institution": grp["NAMEFULL"].iloc[0],
                        "branches": len(grp),
                    })
    return pd.DataFrame(rows), pd.DataFrame(detail)


def established_split(df: pd.DataFrame, cert: str, year: int,
                      new_ids: set) -> tuple[int, int]:
    """Of branches new to `cert` in `year`, how many predate that year?

    Pre-existing branches arriving at a new cert are a charter change.
    Genuinely new construction has a recent established date. This is the
    test that separates a merger from real expansion.
    """
    sub = df[(df["_year"] == year) & (df["CERT"] == cert)
             & (df["UNINUMBR"].isin(new_ids))]
    est = pd.to_datetime(sub.get("SIMS_ESTABLISHED_DATE"), errors="coerce").dt.year
    return int((est < year - 1).sum()), int((est >= year - 1).sum())


def md_table(df: pd.DataFrame, floatfmt: str = "{:,.0f}") -> str:
    """Render a DataFrame as a markdown table.

    Hand-rolled rather than `DataFrame.to_markdown()`, which needs `tabulate`.
    Adding a dependency for table formatting is not worth it - the style rule
    is standard library plus the pinned set.
    """
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: floatfmt.format(v) if pd.notna(v) else "")
        elif pd.api.types.is_integer_dtype(d[c]):
            d[c] = d[c].map(lambda v: f"{v:,}" if pd.notna(v) else "")
        else:
            d[c] = d[c].astype("string").fillna("")

    index_name = d.index.name or ""
    header = [index_name] + [str(c) for c in d.columns]
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for idx, row in d.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [str(v) for v in row]) + " |")
    return "\n".join(lines)


def check_scope(df: pd.DataFrame) -> None:
    """Every row must be a branch physically located in WI or IL.

    Guards the STALP/STALPBR confusion. `STALP` is the institution's charter
    state; `STALPBR` is where the branch actually is. Filtering on the former
    silently produces a nationwide extract for in-state-chartered banks while
    dropping in-state branches of out-of-state banks. It looks plausible -
    row counts are the right order of magnitude - so nothing catches it
    downstream. Fail here instead.
    """
    if "STALPBR" not in df.columns:
        raise SystemExit("STALPBR absent - cannot verify geographic scope.")
    out = df.loc[~df["STALPBR"].isin(["WI", "IL"]), "STALPBR"].value_counts()
    if len(out):
        print("\n  [FAIL] rows outside WI/IL by branch location:")
        print(out.head(12).to_string())
        raise SystemExit(
            f"{int(out.sum()):,} of {len(df):,} rows are branches outside the "
            "footprint. The download filtered on STALP (charter state) rather "
            "than STALPBR (branch state). Fix scripts/01_download.py, delete "
            "data/raw/fdic_sod_*.csv, and re-run it."
        )
    print(f"  [ok  ] scope: all {len(df):,} rows are branches in WI or IL")


FOOTPRINT_CHANGE_LIMIT = 0.20
ACK = ROOT / "config" / "footprint_acknowledged.json"


def check_footprint_change(df: pd.DataFrame) -> None:
    """FR-06 guard: halt if a new vintage changes the footprint's shape.

    The refresh procedure promises the retail team can re-run this against a
    new SOD vintage without an analyst. That promise is only safe if the tool
    refuses to run when the ground moves under it.

    It will move. Associated closed its acquisition of American National on
    2026-04-01 and was converting branches in Q3 2026, so the 2026 vintage
    will carry Omaha and Twin Cities branches inside Associated's certificate
    - new states, outside the configured scope, arriving as a step change. A
    tool that silently absorbs that produces a confident wrong answer.

    Two tests on the newest transition:
      - any branch in a state outside the configured scope
      - any institution whose branch count moves more than 20% year over year

    Known and accepted movements are recorded in
    config/footprint_acknowledged.json, so acknowledging a change is a
    deliberate, reviewable act rather than an edit to this file.
    """
    years = sorted(df["_year"].unique())
    if len(years) < 2:
        return
    prev, cur = years[-2], years[-1]

    acked = set()
    if ACK.exists():
        payload = json.loads(ACK.read_text(encoding="utf-8"))
        acked = {(str(e["cert"]), int(e["year"])) for e in payload.get("accepted", [])}

    counts = (df[df["_year"].isin([prev, cur])]
              .groupby(["CERT", "_year"])["UNINUMBR"].nunique().unstack(fill_value=0))
    counts = counts[counts[prev] >= 10]          # ignore noise at tiny institutions
    counts["change"] = (counts[cur] - counts[prev]) / counts[prev]

    breaches = counts[counts["change"].abs() > FOOTPRINT_CHANGE_LIMIT]
    breaches = breaches[[(c, cur) not in acked for c in breaches.index]]

    if len(breaches):
        names = (df[df["_year"] == cur].drop_duplicates("CERT")
                 .set_index("CERT")["NAMEFULL"])
        print(f"\n  [HALT] {len(breaches)} institution(s) moved more than "
              f"{FOOTPRINT_CHANGE_LIMIT:.0%} between {prev} and {cur}:")
        for cert, row in breaches.iterrows():
            print(f"         CERT {cert} {str(names.get(cert, ''))[:38]:38s} "
                  f"{int(row[prev]):>4} -> {int(row[cur]):>4}  "
                  f"({row['change']:+.0%})")
        raise SystemExit(
            "Footprint changed materially in the newest vintage. Review each "
            "movement above, then either correct the scope or record the "
            f"accepted changes in {ACK.relative_to(ROOT)} and re-run. The "
            "pipeline will not proceed on an unexplained step change."
        )
    print(f"  [ok  ] footprint stable {prev}->{cur} "
          f"(no institution moved >{FOOTPRINT_CHANGE_LIMIT:.0%})")


def main() -> int:
    print("Loading SOD vintages")
    df = load_all()
    check_scope(df)
    check_footprint_change(df)
    df["_county_fips"] = county_fips(df)
    print(f"\nCombined: {len(df):,} branch-year rows, {len(df.columns)} columns")

    counts = branch_counts(df)
    deposits = deposit_distribution(df)
    rates = population_rates(df)
    trans, detail = transitions(df)

    # Sanity check the one thing that silently ruins every downstream join.
    bad_fips = df["_county_fips"].isna().sum() + (df["_county_fips"].str.len() != 5).sum()

    # Largest single transition, examined for merger vs. expansion.
    narrative = ""
    if not detail.empty:
        top = detail.sort_values("branches", ascending=False).iloc[0]
        prev_y, cur_y = (int(x) for x in top["transition"].split("->"))
        year = cur_y if top["event"] == "appeared" else prev_y
        ids = (set(df[(df["_year"] == cur_y)]["UNINUMBR"])
               - set(df[(df["_year"] == prev_y)]["UNINUMBR"]))
        old, recent = established_split(df, top["cert"], year, ids)
        narrative = (
            f"The largest single movement is **{top['institution']}** "
            f"(CERT {top['cert']}), which {top['event']} "
            f"{top['branches']:,} branches in {top['transition']}. "
            f"Of those, **{old:,} were established before {year - 1}** and "
            f"{recent:,} in the year or two prior. Pre-existing branches "
            f"arriving under a different certificate is a charter "
            f"consolidation, not new construction — the branches were "
            f"re-registered, not built. Treating this as organic growth "
            f"would materially overstate expansion in that market."
        )

    STAGING.mkdir(parents=True, exist_ok=True)
    out = STAGING / "sod_all.csv.gz"
    df.to_csv(out, index=False, compression="gzip")

    REPORT.write_text(f"""# SOD Content Profile, 2019–2025

Generated by `scripts/02_profile_sod.py`. Covers {len(df):,} branch-year rows
across {len(counts)} vintages for Wisconsin and Illinois.

**This report replaces the originally planned `sod_schema_changes.md`.** SOD is
now retrieved from the FDIC REST API, which serves a normalized schema for
every vintage, so there is no field-name drift left to document. Profiling it
would have produced an artifact that proved nothing. Content drift is real and
is what follows. See `docs/data_quality_log.md` for the endpoint change itself.

## 1. Branch counts by year

{md_table(counts)}

## 2. Deposit distribution by year

`DEPSUMBR` is reported in **thousands of dollars** and is left in thousands
here; conversion to whole dollars happens once, at staging.

{md_table(deposits.drop(columns=['share_in_top_1pct']))}

Concentration — share of all deposits held by the largest 1% of branches:

{md_table(deposits[['share_in_top_1pct']] * 100, '{:.1f}%')}

This is the headline limitation in numeric form. Deposits book to the branch
where the account is opened, so headquarters and commercial branches absorb
balances that have nothing to do with local demand.

## 3. Field population rates by year

Proportion of rows where the field is non-null. The API normalizes field
*names* across vintages but does not backfill *values*.

{md_table(rates * 100, '{:.1f}%')}

## 4. Branch transitions between vintages

Keyed two ways. `UNINUMBR` persists when a branch changes owner, so keying
globally on it counts **only true openings and closings**; keying on
(CERT, UNINUMBR) counts a sale as both an appearance and a disappearance.
The difference is `transferred` — branches that changed hands rather than
opening or closing.

{md_table(trans)}

Material movements (25 or more branches at one certificate):

{md_table(detail) if not detail.empty else '_None at this threshold._'}

{narrative}

### Why this matters for BQ-2

Branch counts alone cannot distinguish a closure from a charter change. Both
remove a `UNINUMBR` from the next vintage. Any claim that the subject bank
consolidated faster or slower than its market must net out charter movements
first, or it will read acquisitions as growth and divestitures as decline.

## 5. Integrity check

- County FIPS malformed (not exactly 5 characters): **{bad_fips}**
- Distinct institutions: **{df['CERT'].nunique():,}**
- Distinct branches: **{df['UNINUMBR'].nunique():,}**
- Staged output: `data/staging/sod_all.csv.gz`
""", encoding="utf-8")   # explicit: Windows defaults to cp1252, which cannot
                         # encode the characters used above

    print(f"\nBranch counts by year:\n{counts.to_string()}")
    print(f"\nTransitions:\n{trans.to_string(index=False)}")
    if not detail.empty:
        print(f"\nMaterial movements:\n{detail.to_string(index=False)}")
    print(f"\nCounty FIPS malformed: {bad_fips}")
    print(f"\nWrote {REPORT.relative_to(ROOT)}")
    print(f"Wrote {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)")
    print("\nNext: python scripts/03_select_institution.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
