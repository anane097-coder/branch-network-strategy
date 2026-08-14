"""
06_stage_hmda.py — aggregate loan-level HMDA to fact_tract_lending.

Writes data/staging/fact_tract_lending.csv and docs/hmda_action_profile.md.

GRAIN: tract_geoid x lei x loan_purpose x action_taken

ACTION_TAKEN STAYS IN THE GRAIN, DELIBERATELY.
---------------------------------------------
It would be tidier to pre-aggregate to origination_count, denial_count and so
on, and it would be a mistake. Eight distinct outcomes live in that column and
they are not interchangeable:

    1  Loan originated
    2  Application approved but not accepted
    3  Application denied
    4  Application withdrawn by applicant
    5  File closed for incompleteness
    6  Purchased loan          <-- bought, NOT originated
    7  Preapproval request denied
    8  Preapproval request approved but not accepted

Keeping action_taken in the grain means no downstream query can count
"originations" without naming `action_taken = 1`. Pre-aggregating would bury
that decision inside this script where nobody reviewing a SQL file would see
it. The measures here are therefore deliberately dumb - a record count and an
amount sum - and every meaningful ratio is defined in SQL against explicit
codes.

WHY CODE 6 IS THE DANGEROUS ONE
-------------------------------
A purchased loan is one the institution BOUGHT on the secondary market. It
says nothing about whether that institution lends in the tract. Observed in
this vintage:

  - purchased loans are 10.4% of all records (73,049 of 700,896)
  - counting them as originations overstates lending by 18.9%
  - no filter at all overstates it by 81.0%

And the distortion is wildly uneven, which is worse than a uniform bias.
Associated is 0.8% purchased. Several lenders in this file are 88-99%
purchased - one shows 1,252 purchased against 13 originated. Those are
secondary-market aggregators, not local lenders. Sweeping code 6 into a
capture-rate denominator would inflate competitors by two orders of magnitude
more than the subject, make Associated's share look far worse than it is, and
drive BQ-4 toward unmet demand that does not exist.

Excluding code 6 costs 8 tracts of coverage out of 4,773. It is not a
trade-off.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "hmda_action_profile.md"
OUT = STAGING / "fact_tract_lending.csv"

SUBJECT_LEI = "ZF85QS7OXKPBG52R7N18"

ACTION_TAKEN = {
    "1": ("Loan originated", "origination"),
    "2": ("Application approved but not accepted", "application"),
    "3": ("Application denied", "application"),
    "4": ("Application withdrawn by applicant", "application"),
    "5": ("File closed for incompleteness", "application"),
    "6": ("Purchased loan", "purchased"),
    "7": ("Preapproval request denied", "preapproval"),
    "8": ("Preapproval request approved but not accepted", "preapproval"),
}

LOAN_PURPOSE = {
    "1": "Home purchase", "2": "Home improvement", "31": "Refinancing",
    "32": "Cash-out refinancing", "4": "Other purpose", "5": "Not applicable",
}


def md(df: pd.DataFrame, fmt: str = "{:,}") -> str:
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for i, r in df.iterrows():
        out.append("| " + " | ".join([str(i)] + [
            fmt.format(v) if isinstance(v, (int, float)) else str(v)
            for v in r]) + " |")
    return "\n".join(out)


def main() -> int:
    lar = pd.read_csv(
        RAW / "hmda_tract_subset_2025_wi_il.csv.gz",
        usecols=["census_tract", "lei", "loan_purpose", "action_taken",
                 "loan_amount", "state_code", "business_or_commercial_purpose"],
        dtype="string")
    print(f"Loan-level records: {len(lar):,}")

    before = len(lar)
    lar = lar.dropna(subset=["census_tract"])
    dropped = before - len(lar)
    if dropped:
        print(f"  [note] {dropped:,} records carry no census tract and cannot "
              f"be placed geographically")

    bad = set(lar["action_taken"].dropna().unique()) - set(ACTION_TAKEN)
    if bad:
        raise SystemExit(
            f"Unmapped action_taken codes present: {sorted(bad)}. Every code "
            "must have a documented meaning before aggregation - an unmapped "
            "value would be swept into whatever filter happens to catch it.")
    print(f"  [ok  ] all action_taken codes are mapped")

    lar["amount"] = pd.to_numeric(lar["loan_amount"], errors="coerce")

    fact = (lar.groupby(["census_tract", "lei", "loan_purpose", "action_taken"],
                        dropna=False)
               .agg(record_count=("lei", "size"),
                    total_amount=("amount", "sum"))
               .reset_index()
               .rename(columns={"census_tract": "tract_geoid"}))
    fact["action_class"] = fact["action_taken"].map(
        lambda c: ACTION_TAKEN.get(c, ("", "unmapped"))[1])

    STAGING.mkdir(parents=True, exist_ok=True)
    fact.to_csv(OUT, index=False)
    print(f"\nfact_tract_lending: {len(fact):,} rows at "
          f"tract x lei x loan_purpose x action_taken")

    # --- profile ---------------------------------------------------------
    dist = lar["action_taken"].value_counts().sort_index()
    prof = pd.DataFrame({
        "meaning": [ACTION_TAKEN[c][0] for c in dist.index],
        "class": [ACTION_TAKEN[c][1] for c in dist.index],
        "records": dist.values,
    }, index=dist.index)
    prof.index.name = "code"
    prof["share"] = (prof["records"] / len(lar) * 100).round(1)

    subj = lar[lar["lei"] == SUBJECT_LEI]
    sd = subj["action_taken"].value_counts().sort_index()

    orig = int((lar["action_taken"] == "1").sum())
    purch = int((lar["action_taken"] == "6").sum())
    apps = int(lar["action_taken"].isin(["1", "2", "3", "4", "5"]).sum())

    o = lar[lar["action_taken"] == "1"].groupby("lei").size().rename("originated")
    p = lar[lar["action_taken"] == "6"].groupby("lei").size().rename("purchased")
    cmp = pd.concat([o, p], axis=1).fillna(0).astype(int)
    cmp = cmp[cmp.sum(axis=1) >= 200]
    cmp["pct_purchased"] = (cmp["purchased"] / cmp.sum(axis=1) * 100).round(1)
    top = cmp.sort_values("pct_purchased", ascending=False).head(6)
    subj_pct = (cmp.loc[SUBJECT_LEI, "pct_purchased"]
                if SUBJECT_LEI in cmp.index else float("nan"))

    biz = lar["business_or_commercial_purpose"].value_counts()

    REPORT.write_text(f"""# HMDA Action-Taken Profile

Generated by `scripts/06_stage_hmda.py`, verified **before** aggregation.

`action_taken` carries eight distinct outcomes in one column. They are not
interchangeable, and a loose filter sweeps them together silently.

{md(prof)}

## What a loose filter costs

| Filter | Records | vs. originations |
|---|---|---|
| Originations only — `action_taken = 1` | {orig:,} | — |
| Plus purchased loans (6) | {orig + purch:,} | **+{purch / orig * 100:.1f}%** |
| Applications, the demand universe (1–5) | {apps:,} | +{(apps - orig) / orig * 100:.1f}% |
| No filter at all | {len(lar):,} | **+{(len(lar) - orig) / orig * 100:.1f}%** |

## Why purchased loans are the dangerous code

A purchased loan was **bought on the secondary market**, not originated. It
carries no information about whether that lender serves the tract.

The distortion is not uniform, which is what makes it worse than a simple
overstatement. Lenders most affected, against the subject:

{md(top)}

**Associated Bank is {subj_pct:.1f}% purchased.** Several lenders here exceed
88%; these are secondary-market aggregators rather than local lenders.
Including code 6 in a capture-rate denominator would inflate competitors by
roughly two orders of magnitude more than the subject, make Associated's
share look far worse than it is, and push BQ-4 toward unmet demand that does
not exist.

Excluding code 6 costs **8 tracts of coverage out of 4,773**. It is not a
trade-off.

## Subject profile

{md(pd.DataFrame({"meaning": [ACTION_TAKEN[c][0] for c in sd.index],
                  "records": sd.values},
                 index=pd.Index(sd.index, name="code")))}

Associated reports **no preapproval records** (codes 7 and 8) in this vintage.

## Business- and commercial-purpose lending

`business_or_commercial_purpose` distinguishes loans secured by a dwelling but
made for business purposes. These are dwelling-secured commercial credit, not
household mortgage demand, and BQ-4 should say which it is measuring.

{md(biz.to_frame("records"))}

Codes: `1` primarily business or commercial · `2` not primarily business or
commercial · `1111` exempt under the partial exemption, which itself indicates
a low-volume filer.

## How the grain protects this

`fact_tract_lending` keeps `action_taken` **in the grain** rather than
pre-aggregating to origination and denial counts. No downstream query can
count originations without naming `action_taken = 1`, so the decision is
visible in the SQL where a reviewer reads it, instead of buried in this
script where nobody would.

Measures are deliberately minimal — `record_count` and `total_amount`. Every
meaningful ratio is defined in SQL against explicit codes.
""", encoding="utf-8")

    print(f"\n{prof.to_string()}")
    print(f"\nSubject purchased share: {subj_pct:.1f}%")
    print(f"\nWrote {OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: python scripts/07_spatial_join.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
