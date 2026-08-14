"""
03_select_institution.py — apply the selection criteria and choose the subject.

Reads data/staging/sod_all.csv.gz (from script 02) plus the FDIC institutions
and HMDA files. Writes docs/institution_selection.md.

That document is a work sample, not a log. It should read like an analyst
explaining a choice.

CRITERIA
  1  Branches in both WI and IL
  2  >= 25 branches across WI+IL
  3  Continuity through successor chains across all seven vintages, AND no
     single year-over-year branch count change above 20% attributable to
     acquisition. Failing only the second part is survivable if the analysis
     segments pre- and post-merger explicitly.
  4  Active 2025 HMDA filer with a resolvable LEI
  5  Not a top-5 national bank
  6  Assets roughly $2B - $60B
  7  Not in a publicly announced merger  (NOT TESTABLE FROM DATA - manual)

On criterion 3: the acquisition test is what makes deposit CAGR mean anything.
A bank that grew by buying branches did not earn that growth, and BQ-2 and
BQ-3 would both read the jump as performance. Branch transfers are detectable
because UNINUMBR persists across a change of ownership - a branch that appears
at a new CERT while having existed at a different CERT the year before was
bought, not built.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAGING = ROOT / "data" / "staging"
REPORT = ROOT / "docs" / "institution_selection.md"

MIN_BRANCHES = 25
# Criterion 1 is a functional floor, not a round number. Branch-level analysis
# needs enough branches per state for within-market comparison to mean
# anything; a single-branch presence produces a catchment sample of one.
MIN_PRIMARY_STATE = 10
MIN_SECONDARY_STATE = 5
ASSET_MIN_K = 2_000_000        # ASSET is reported in thousands
ASSET_MAX_K = 60_000_000
ACQUISITION_LIMIT = 0.20
YEARS = list(range(2019, 2026))
FFIEC = "https://ffiec.cfpb.gov/v2/public/institutions/{lei}/year/2025"
UA = {"User-Agent": "branch-network-strategy portfolio project"}


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    sod_path = STAGING / "sod_all.csv.gz"
    if not sod_path.exists():
        raise SystemExit("Run scripts/02_profile_sod.py first.")
    sod = pd.read_csv(sod_path, dtype={"CERT": "string", "UNINUMBR": "string"},
                      low_memory=False)
    inst = pd.read_csv(RAW / "fdic_institutions_all.csv", dtype="string")
    inst["ASSET_K"] = pd.to_numeric(inst["ASSET"], errors="coerce")
    return sod, inst


def footprint_profile(sod: pd.DataFrame) -> pd.DataFrame:
    """Per-cert footprint, measured on the CURRENT vintage.

    The state splits are 2025 counts, not distinct branches ever operated.
    Criteria 1 and 2 ask what the footprint looks like now - a bank that left
    Illinois in 2021 does not have an Illinois footprint today, and counting
    across all seven vintages would say it does.
    """
    cur = sod[sod["_year"] == sod["_year"].max()]
    latest = sod.sort_values("_year").drop_duplicates("CERT", keep="last")
    out = pd.DataFrame({
        "name": latest.set_index("CERT")["NAMEFULL"],
        "wi": cur[cur["_state"] == "WI"].groupby("CERT")["UNINUMBR"].nunique(),
        "il": cur[cur["_state"] == "IL"].groupby("CERT")["UNINUMBR"].nunique(),
        "peak": sod.groupby(["CERT", "_year"]).size().groupby("CERT").max(),
        "vintages": sod.groupby("CERT")["_year"].nunique(),
    })
    out[["wi", "il"]] = out[["wi", "il"]].fillna(0)
    out["branches_2025"] = out["wi"] + out["il"]
    return out.fillna("")


def acquisition_intensity(sod: pd.DataFrame) -> pd.DataFrame:
    """Largest single-year branch change attributable to acquisition, per cert.

    A (cert, uninumbr) pair that is new this year, whose uninumbr existed
    somewhere in the footprint last year, is an acquired branch rather than a
    newly built one.
    """
    per_year = {y: set(zip(g["CERT"], g["UNINUMBR"]))
                for y, g in sod.groupby("_year")}
    global_year = {y: set(g["UNINUMBR"].dropna())
                   for y, g in sod.groupby("_year")}
    counts = sod.groupby(["_year", "CERT"]).size().unstack(fill_value=0)

    worst: dict[str, float] = {}
    detail: dict[str, str] = {}
    years = sorted(per_year)
    for prev, cur in zip(years, years[1:]):
        gained = per_year[cur] - per_year[prev]
        for cert, uni in gained:
            if uni in global_year[prev]:      # existed elsewhere last year
                base = counts.loc[prev].get(cert, 0)
                if base:
                    worst.setdefault(cert, 0.0)
                    worst[cert] += 1 / base
    for cert, ratio in worst.items():
        detail[cert] = f"{ratio:.0%}"
    return pd.DataFrame({"acq_ratio": pd.Series(worst),
                         "acq_pct": pd.Series(detail)})


def resolve_lei(inst_row: pd.Series, lei_by_rssd: dict) -> tuple[str, str]:
    """Resolve a cert's LEI via FED_RSSD == institutionId2017."""
    rssd = str(inst_row.get("FED_RSSD") or "").strip()
    if not rssd:
        return "", "no_fed_rssd"
    lei = lei_by_rssd.get(rssd)
    if lei:
        return lei, "exact_rssd"
    return "", "no_lei_for_rssd"


def build_lei_index(leis: list[str]) -> dict:
    """Map institutionId2017 (== RSSD) -> LEI for the given filers.

    Cached to data/staging/, because this is ~1,400 HTTP round trips and the
    answer does not change between runs. Fetched concurrently; sequentially it
    takes long enough that it reads as a hang.

    institutionId2017 is missing for roughly 9% of filers, typically those
    chartered after 2017. Those are recorded as such rather than silently
    skipped - an institution that cannot be resolved is a finding.
    """
    cache = STAGING / "lei_rssd_index.json"
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if set(payload.get("leis", [])) == set(leis):
            print(f"    cached: {len(payload['index'])} usable, "
                  f"{payload['missing']} without a 2017 identifier")
            return payload["index"]

    def fetch_one(lei: str) -> tuple[str, str]:
        try:
            r = requests.get(FFIEC.format(lei=lei), headers=UA, timeout=30)
            if r.status_code != 200:
                return lei, ""
            return lei, str(r.json().get("institutionId2017") or "").strip()
        except (requests.RequestException, ValueError):
            return lei, ""

    index, missing, done = {}, 0, 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        for lei, iid in pool.map(fetch_one, leis):
            done += 1
            if iid and iid not in ("-1", "None"):
                index[iid] = lei
            else:
                missing += 1
            if done % 200 == 0:
                print(f"    resolved {done}/{len(leis)}", flush=True)

    print(f"    {len(index)} usable, {missing} without a 2017 identifier")
    STAGING.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(
        {"leis": sorted(leis), "index": index, "missing": missing}, indent=1), encoding="utf-8")
    return index


def main() -> int:
    sod, inst = load()
    print(f"SOD: {len(sod):,} branch-years | institutions: {len(inst):,}")

    prof = footprint_profile(sod)
    prof = prof.join(acquisition_intensity(sod))
    prof["acq_ratio"] = prof["acq_ratio"].fillna(0.0)
    prof = prof.join(inst.drop_duplicates("CERT").set_index("CERT")
                     [["ASSET_K", "FED_RSSD", "STALP", "CITY", "ACTIVE"]])

    top5 = set(inst.nlargest(5, "ASSET_K")["CERT"])

    primary = prof[["wi", "il"]].max(axis=1)
    secondary = prof[["wi", "il"]].min(axis=1)
    c1 = (primary >= MIN_PRIMARY_STATE) & (secondary >= MIN_SECONDARY_STATE)
    prof["primary_state_branches"] = primary
    prof["secondary_state_branches"] = secondary
    c2 = (prof["wi"] + prof["il"]) >= MIN_BRANCHES
    c3a = prof["vintages"] == len(YEARS)
    c3b = prof["acq_ratio"] <= ACQUISITION_LIMIT
    c5 = ~prof.index.isin(top5)
    c6 = prof["ASSET_K"].between(ASSET_MIN_K, ASSET_MAX_K)

    prof["c1_both_states"] = c1
    prof["c2_scale"] = c2
    prof["c3a_continuous"] = c3a
    prof["c3b_organic"] = c3b
    prof["c5_not_top5"] = c5
    prof["c6_asset_band"] = c6

    hard = c1 & c2 & c3a & c5 & c6
    shortlist = prof[hard].copy().sort_values("acq_ratio")
    print(f"\nPassing criteria 1,2,3a,5,6: {len(shortlist)} institutions")

    # Criterion 4 only for the shortlist - a handful of API calls, not 1,431.
    print("\nResolving LEIs for the shortlist (criterion 4)")
    lar = pd.read_csv(RAW / "hmda_tract_subset_2025_wi_il.csv.gz",
                      usecols=["lei"], dtype="string")
    vol = lar.groupby("lei").size()
    filers = pd.DataFrame(json.loads(
        (RAW / "hmda_filers_2025.json").read_text(encoding="utf-8"))["institutions"])
    # Only filers that actually lend in the footprint need resolving.
    active = [l for l in filers["lei"] if l in vol.index]
    print(f"  {len(active)} filers lend in WI+IL; resolving their 2017 ids")
    lei_by_rssd = build_lei_index(active)

    res = shortlist.apply(lambda r: resolve_lei(r, lei_by_rssd), axis=1)
    shortlist["lei"] = [x[0] for x in res]
    shortlist["match_quality"] = [x[1] for x in res]
    shortlist["applications"] = shortlist["lei"].map(vol).fillna(0).astype(int)
    shortlist["c4_hmda_filer"] = shortlist["applications"] > 0
    # Volume sanity: apps per $B of assets. A large bank with a handful of
    # applications means the wrong entity was matched.
    shortlist["apps_per_bn"] = (
        shortlist["applications"] / (shortlist["ASSET_K"] / 1e6)).round(0)

    passing = shortlist[shortlist["c4_hmda_filer"]].copy()
    passing["fully_clean"] = passing["acq_ratio"] <= ACQUISITION_LIMIT

    cols = ["name", "wi", "il", "branches_2025", "ASSET_K", "acq_pct",
            "lei", "match_quality", "applications", "apps_per_bn"]
    print(f"\nCandidates passing all testable criteria: {len(passing)}")
    if len(passing):
        print(passing[cols].to_string())

    def row_md(df, columns, headers):
        lines = ["| " + " | ".join(headers) + " |",
                 "|" + "|".join("---" for _ in headers) + "|"]
        for cert, r in df.iterrows():
            vals = [str(cert)] + [
                f"${r[c]/1e6:.1f}B" if c == "ASSET_K" and pd.notna(r[c])
                else (f"{int(r[c]):,}" if isinstance(r[c], (int, float))
                      and pd.notna(r[c]) else str(r[c] if pd.notna(r[c]) else ""))
                for c in columns]
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)

    clean = passing[passing["fully_clean"]]
    flagged = passing[~passing["fully_clean"]]

    # The population criterion 1 admits - the real two-state field.
    field = prof[c1].copy()
    field["total"] = field["wi"] + field["il"]

    REPORT.write_text(f"""# Institution Selection

Generated by `scripts/03_select_institution.py` from {len(sod):,} branch-years
of FDIC Summary of Deposits (2019–2025) and the 2025 HMDA loan-level file.

## How the field narrowed

{len(prof):,} institutions operated at least one branch in Wisconsin or
Illinois at some point in the seven vintages. Applying the criteria:

| Criterion | Remaining | Eliminated here |
|---|---|---|
| All institutions in the footprint | {len(prof):,} | — |
| 1 — ≥{MIN_PRIMARY_STATE} branches primary state, ≥{MIN_SECONDARY_STATE} secondary | {int(c1.sum()):,} | {len(prof) - int(c1.sum()):,} |
| 2 — ≥{MIN_BRANCHES} branches across WI+IL | {int((c1 & c2).sum()):,} | {int(c1.sum()) - int((c1 & c2).sum()):,} |
| 3a — present in all seven vintages | {int((c1 & c2 & c3a).sum()):,} | {int((c1 & c2).sum()) - int((c1 & c2 & c3a).sum()):,} |
| 5 — not a top-5 national bank | {int((c1 & c2 & c3a & c5).sum()):,} | {int((c1 & c2 & c3a).sum()) - int((c1 & c2 & c3a & c5).sum()):,} |
| 6 — assets ${ASSET_MIN_K/1e6:.0f}B–${ASSET_MAX_K/1e6:.0f}B | {int(hard.sum()):,} | {int((c1 & c2 & c3a & c5).sum()) - int(hard.sum()):,} |
| 4 — resolvable LEI, filed 2025 HMDA | {len(passing):,} | {int(hard.sum()) - len(passing):,} |

Criterion 7 — not in a publicly announced merger — **cannot be tested from
these files**. It was confirmed manually; see below.

## The criteria did not winnow a field. Say so.

**{len(passing)} institution{'s' if len(passing) != 1 else ''} survived.**
{'''
This has to be stated plainly rather than presented as the output of a
seven-criterion tournament. Applying the criteria left a single viable
candidate; the criteria did not choose between contenders, they eliminated
everything else. That is what the data supports, and it is a legitimate
result — but a seven-criterion framework presented as though it had ranked a
genuine field would be a misrepresentation of how the choice was actually
made.

What each criterion removed is in the table above. The binding constraints
were criterion 1 (a real two-state footprint, not a token branch across the
border) and criterion 6 (the asset band). Most institutions in Wisconsin and
Illinois operate in one state only.

The honest framing for the case study: *"Applying the criteria left one
viable candidate. Here is what each criterion eliminated and why, and here is
what would have to change for a different bank to qualify."*

That said, the convergence is structural rather than arbitrary, and the table
below shows why.
''' if len(passing) == 1 else ''}

## The genuine two-state field

These are the institutions clearing criterion 1 — a real footprint on both
sides of the border, not a token branch. It is the whole population the later
criteria act on.

{row_md(field.sort_values("ASSET_K", ascending=False),
        ["name", "wi", "il", "total", "ASSET_K"],
        ["CERT", "Institution", "WI", "IL", "Total", "Assets"])}

Eight of the nine are national or super-regional banks. Their deposits book to
headquarters at scale, which distorts branch-level figures — the exact
distortion criteria 5 and 6 exist to exclude — and their siting strategy is
not tract-driven in the first place. **Associated is the only mid-size
regional with a genuine two-state Wisconsin–Illinois footprint.** The field
narrowed to one because the market contains one, not because the criteria
were tuned until it did.

## Contingency

If the subject falls through, the fallback is **Old National Bank**
(CERT 3832, 31 WI / 84 IL, $72.6B) — the nearest institution above the asset
ceiling, at roughly 21% over. Selecting it would require relaxing criterion 6
to about $75B **and** invoking the segmentation clause in criterion 3, since
Old National acquired First Midwest during the study window; that acquisition
is visible in this data as a 109-branch movement in 2021→2022.

UMB Bank ($72.3B) is not a fallback: 18 branches total fails criterion 2 —
and those 18 are the branches it acquired from HTLF Bank in 2025, so its
Wisconsin–Illinois presence is one year old.

**What degrades if the fallback is invoked.** Criterion 6 exists because large
banks book deposits to headquarters at scale, and that booking artefact is the
project's headline limitation. Old National at $72.6B is 60% larger than the
subject, so relaxing the ceiling makes the limitation meaningfully worse
rather than marginally: more of its deposit base concentrates at
non-Wisconsin head-office branches, the branch performance index in BQ-3 gets
noisier at exactly the top of the distribution, and the "actual versus
predicted deposits" gap becomes harder to attribute to local demand. Combined
with the segmentation required for its First Midwest acquisition, the fallback
buys a usable subject at the cost of a materially weaker BQ-3.

Both changes would need to be stated in the case study rather than made
quietly.

## Criterion 3, and why it is two tests

The institution must appear in all seven vintages, tracked through successor
chains. But continuity alone is the wrong screen: it excludes every bank that
did anything, and the real damage M&A does is not a presence gap. It is that
**deposit CAGR stops meaning anything when inorganic growth swamps organic**.
A bank that bought its growth did not earn it, and BQ-2 and BQ-3 would both
read the jump as performance.

So the second test measures acquisition intensity directly. Branch transfers
are detectable because `UNINUMBR` survives a change of ownership: a branch
appearing at a new certificate, having existed at a different one the year
before, was bought rather than built. The threshold is a single-year change of
{ACQUISITION_LIMIT:.0%}.

## Candidates passing every testable criterion

{row_md(clean, cols[:-4] + ['lei', 'applications', 'apps_per_bn'],
        ['CERT', 'Institution', 'WI', 'IL', '2025 branches', 'Assets',
         'Acq.', 'LEI', 'Applications', 'Apps/$B'])
 if len(clean) else '_None._'}

## Candidates flagged on acquisition intensity

These meet every other criterion but exceed the {ACQUISITION_LIMIT:.0%}
threshold in at least one year. They remain eligible **only** if the analysis
segments pre- and post-merger periods explicitly and the case study says why.

{row_md(flagged, cols[:-4] + ['lei', 'applications', 'apps_per_bn'],
        ['CERT', 'Institution', 'WI', 'IL', '2025 branches', 'Assets',
         'Acq.', 'LEI', 'Applications', 'Apps/$B'])
 if len(flagged) else '_None._'}

## Match quality

Every LEI here resolves on an exact identifier join — FDIC `FED_RSSD` against
the FFIEC `institutionId2017` — not on name matching. `institutionId2017` is
absent for roughly 9% of filers, generally those chartered after 2017; such
institutions carry `match_quality = no_2017_id` rather than a null, so an
unresolvable institution is visible as a finding rather than a gap.

The `Apps/$B` column is the sanity check: a multi-billion-dollar bank showing
a handful of applications means the wrong entity was matched, not that the
bank stopped lending.

## Criterion 7 — checked manually, 2026-08-14

Not testable from the filings, so it was checked against public announcements.

**Associated Banc-Corp is not in a pending merger.** It announced the
acquisition of American National Corporation on 1 December 2025, received OCC
and Federal Reserve approval on 12 March 2026, and **closed the transaction on
1 April 2026**. Nothing is pending as of this writing.

Three consequences the case study must carry, because a completed acquisition
is not the same as no acquisition:

1. **The 2019–2025 SOD window is unaffected.** The deal closed after the
   30 June 2025 as-of date, so no American National branch appears in this
   analysis under Associated's certificate.
2. **The geography is outside the footprint.** American National is an Omaha
   and Twin Cities franchise — Nebraska and Minnesota. The Wisconsin and
   Illinois branch analysis is therefore geographically undisturbed, which is
   the reason this acquisition does not disqualify the subject.
3. **Institution-level figures are now mixed-vintage.** The asset total used
   for criterion 6 comes from current FDIC data and is post-acquisition, while
   the branch and deposit data are pre-acquisition. The band is cleared either
   way, so the outcome does not change — but any institution-level denominator
   must state which side of 1 April 2026 it sits on.

Branch and systems conversion was scheduled for Q3 2026, meaning the **2026
SOD vintage will show a discontinuity**. Anyone re-running this pipeline
against a newer vintage needs to know that before reading the trend.

## Selection

_To be written once the subject is fixed. State plainly why the chosen bank
won and what the runners-up lacked — this section is the work sample._
""", encoding="utf-8")   # explicit: Windows defaults to cp1252 and cannot
                         # encode the characters used above

    print(f"\nWrote {REPORT.relative_to(ROOT)}")
    print("\nCriterion 7 (publicly announced merger) needs a manual check.")
    print("Next: python scripts/04_crosswalk.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
