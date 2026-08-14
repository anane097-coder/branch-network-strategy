"""
04_crosswalk.py — build dim_institution_crosswalk (CERT <-> RSSD <-> LEI).

Writes data/staging/dim_institution_crosswalk.csv and
docs/crosswalk_exceptions.md. Gate 5.4 / UAT-10.

THE SUBJECT IS NOT THE INTERESTING PART.
Associated Bank is already verified: CERT 5296, FED_RSSD 917742, LEI
ZF85QS7OXKPBG52R7N18, confirmed independently against GLEIF ("Associated Bank,
National Association", Green Bay US-WI) and present in the 2025 loan-level file
with 9,260 applications. UAT-10 is asserted here, but it is a formality.

The gate's real content is everything that does NOT resolve. Two federal
datasets describe overlapping populations of institutions with no shared key;
the honest output is not a high match rate but a complete, reasoned account of
the failures. Every unmatched institution carries a match_quality naming why.

THE JOIN
    FDIC CERT -> FED_RSSD  ==  institutionId2017 <- HMDA LEI

`institutionId2017` is the FFIEC's legacy 2017 identifier. For FDIC-insured
banks it carries the RSSD, which is why this join works at all - the
documented `rssd` field returns -1 for every institution tested.

NEVER KEY THIS ON CERT. The 2017 ARID was agency-code plus respondent-ID,
where respondent ID meant the FDIC cert for FDIC-supervised banks, the charter
number for OCC-chartered ones, and the RSSD for Fed-supervised ones. A join
that matched the value against CERT would mis-resolve national banks and look
clean doing it: 3 of 120 institutionId2017 values sampled also match some
CERT. Matching the full value against FED_RSSD avoids the trap entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "crosswalk_exceptions.md"
OUT = STAGING / "dim_institution_crosswalk.csv"

SUBJECT_CERT = "5296"
SUBJECT_LEI = "ZF85QS7OXKPBG52R7N18"
SUBJECT_RSSD = "917742"

# Ordered worst-to-best so value_counts reads sensibly in the report.
QUALITY = {
    "exact_rssd": "Resolved. FED_RSSD matched institutionId2017 exactly.",
    "no_lei_for_rssd": "Has an RSSD, but no 2025 HMDA filer reports it. The "
                       "institution did not file, or filed under an affiliate.",
    "no_fed_rssd": "FDIC publishes no FED_RSSD for this certificate. Nothing "
                   "to join on.",
    "not_in_fdic": "Certificate appears in SOD but not in the FDIC "
                   "institutions file.",
}


def main() -> int:
    sod = pd.read_csv(STAGING / "sod_all.csv.gz",
                      dtype={"CERT": "string", "UNINUMBR": "string"},
                      low_memory=False)
    inst = pd.read_csv(RAW / "fdic_institutions_all.csv", dtype="string")
    idx_path = STAGING / "lei_rssd_index.json"
    if not idx_path.exists():
        raise SystemExit("Run scripts/03_select_institution.py first - it "
                         "builds data/staging/lei_rssd_index.json.")
    payload = json.loads(idx_path.read_text(encoding="utf-8"))
    lei_by_rssd: dict = payload["index"]
    n_no_2017 = payload["missing"]

    lar = pd.read_csv(RAW / "hmda_tract_subset_2025_wi_il.csv.gz",
                      usecols=["lei"], dtype="string")
    volume = lar.groupby("lei").size()

    # Every institution operating a branch in the footprint, ever.
    certs = (sod.groupby("CERT")
                .agg(branches_2025=("UNINUMBR", "nunique"),
                     name=("NAMEFULL", "last"))
                .reset_index())
    cur = sod[sod["_year"] == sod["_year"].max()].groupby("CERT")["UNINUMBR"].nunique()
    certs["branches_2025"] = certs["CERT"].map(cur).fillna(0).astype(int)

    fdic = inst.drop_duplicates("CERT").set_index("CERT")
    certs["fed_rssd"] = certs["CERT"].map(fdic["FED_RSSD"])
    certs["in_fdic"] = certs["CERT"].isin(fdic.index)

    def resolve(row) -> tuple[str, str]:
        if not row["in_fdic"]:
            return "", "not_in_fdic"
        rssd = str(row["fed_rssd"] or "").strip()
        if not rssd or rssd.lower() == "nan":
            return "", "no_fed_rssd"
        lei = lei_by_rssd.get(rssd)
        return (lei, "exact_rssd") if lei else ("", "no_lei_for_rssd")

    res = certs.apply(resolve, axis=1)
    certs["lei"] = [r[0] for r in res]
    certs["match_quality"] = [r[1] for r in res]
    certs["match_method"] = certs["match_quality"].map(
        lambda q: "fed_rssd == institutionId2017" if q == "exact_rssd" else "")
    certs["applications_2025"] = certs["lei"].map(volume).fillna(0).astype(int)
    certs["is_subject_bank"] = certs["CERT"] == SUBJECT_CERT

    # --- UAT-10 -----------------------------------------------------------
    subj = certs[certs["CERT"] == SUBJECT_CERT]
    problems = []
    if len(subj) != 1:
        problems.append(f"subject CERT {SUBJECT_CERT} appears {len(subj)} times")
    else:
        s = subj.iloc[0]
        if s["lei"] != SUBJECT_LEI:
            problems.append(f"subject LEI is {s['lei']!r}, expected {SUBJECT_LEI}")
        if str(s["fed_rssd"]) != SUBJECT_RSSD:
            problems.append(f"subject RSSD is {s['fed_rssd']!r}, expected {SUBJECT_RSSD}")
        if s["applications_2025"] <= 0:
            problems.append("subject LEI has no 2025 applications in WI+IL")
    matches = int((certs["lei"] == SUBJECT_LEI).sum())
    if matches != 1:
        problems.append(f"{matches} certificates resolve to the subject LEI, expected 1")

    if problems:
        for p in problems:
            print(f"  [FAIL] UAT-10: {p}")
        raise SystemExit("UAT-10 failed. Gate 5.4 is not passed - return to "
                         "script 03 and reselect. Do not work around this.")
    print(f"  [PASS] UAT-10: CERT {SUBJECT_CERT} -> RSSD {SUBJECT_RSSD} -> "
          f"LEI {SUBJECT_LEI}, {int(subj.iloc[0]['applications_2025']):,} "
          "applications, exactly one match")

    STAGING.mkdir(parents=True, exist_ok=True)
    certs.to_csv(OUT, index=False)

    counts = certs["match_quality"].value_counts()
    resolved = int(counts.get("exact_rssd", 0))
    # Institutions still operating branches are the ones that matter; a cert
    # that left the footprint years ago failing to resolve is not a gap.
    live = certs[certs["branches_2025"] > 0]
    live_res = int((live["match_quality"] == "exact_rssd").sum())

    lines = ["| Institution | CERT | 2025 branches | Reason |", "|---|---|---|---|"]
    for _, r in (live[live["match_quality"] != "exact_rssd"]
                 .sort_values("branches_2025", ascending=False).head(25).iterrows()):
        lines.append(f"| {r['name']} | {r['CERT']} | {r['branches_2025']} "
                     f"| `{r['match_quality']}` |")

    REPORT.write_text(f"""# Institution Crosswalk — Exceptions

Generated by `scripts/04_crosswalk.py`.

FDIC keys institutions on CERT. HMDA keys them on LEI. No shared key exists.
The bridge is `FDIC CERT → FED_RSSD == institutionId2017 ← HMDA LEI`, an exact
identifier join — not name matching.

The published HMDA panel file that was designed to carry this link returns
S3 `AccessDenied`, and the Federal Reserve NPW bulk download is behind a
CAPTCHA. The FFIEC API's documented `rssd` field returns `-1` for every
institution tested. The join works because the legacy `institutionId2017`
field carries the RSSD for FDIC-insured banks.

## UAT-10 — subject institution

**PASS.** Associated Bank resolves to exactly one LEI, present in the 2025
loan-level file.

| | |
|---|---|
| CERT | {SUBJECT_CERT} |
| FED_RSSD | {SUBJECT_RSSD} |
| LEI | `{SUBJECT_LEI}` |
| 2025 applications, WI+IL | {int(subj.iloc[0]['applications_2025']):,} |
| Certificates resolving to this LEI | 1 |

Independently confirmed against GLEIF, which returns the registrant as
"Associated Bank, National Association", Green Bay, US-WI.

## Match rates

{len(certs):,} certificates have operated a branch in the footprint across the
seven vintages; {len(live):,} still operate one in 2025.

| Outcome | All certificates | Still operating in 2025 |
|---|---|---|
| Resolved | {resolved:,} ({resolved/len(certs):.0%}) | {live_res:,} ({live_res/len(live):.0%}) |
| Unresolved | {len(certs)-resolved:,} | {len(live)-live_res:,} |

**A high match rate is not the goal.** Most unresolved institutions are
correctly unresolved: a community bank that files no mortgages has no LEI in
this file, and that is a fact about the bank, not a defect in the join.

## Why each failure happened

| `match_quality` | Count | Meaning |
|---|---|---|
{chr(10).join(f"| `{q}` | {int(counts.get(q, 0)):,} | {d} |" for q, d in QUALITY.items())}

Separately, **{n_no_2017:,} of the {len(payload['leis']):,} filers lending in
WI+IL carry no `institutionId2017` at all** — generally institutions chartered
after 2017, which have no legacy identifier to report. These cannot be reached
from the FDIC side by any identifier route. They are not counted as join
failures above because the failure is in the source data, but they bound what
this crosswalk can ever achieve.

## Unresolved institutions still operating branches

These are the ones worth reading — an institution with branches in the
footprint that cannot be reached from the HMDA side.

{chr(10).join(lines)}

## What this means for BQ-4

Unmet mortgage demand compares the subject's originations against all lenders'
originations in the same tract. That denominator is taken directly from the
loan-level file by tract and needs no crosswalk, so it is unaffected by the
failures above. The crosswalk is required only to identify *which* LEI is the
subject, and for competitor-level attribution where a named institution must
be tied to its branches. Where an institution does not resolve, its lending
still counts toward the tract total; it simply cannot be named.
""", encoding="utf-8")

    print(f"\n  {resolved:,}/{len(certs):,} certificates resolved "
          f"({live_res:,}/{len(live):,} still operating)")
    print(counts.to_string())
    print(f"\nWrote {OUT.relative_to(ROOT)}")
    print(f"Wrote {REPORT.relative_to(ROOT)}")
    print("\nNext: python scripts/05_stage_acs.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
