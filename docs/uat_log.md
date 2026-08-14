# UAT Log

Record every execution: passes, failures, and what fixed the failures.

**Do not sanitize this file.** A log showing only first-run passes reads as weak
testing. Failures found and resolved are the evidence that the testing was real.

| ID | Requirement | Method | Expected | Date | Result | Notes |
|---|---|---|---|---|---|---|
| UAT-01 | AC-01 | Run SQL-14; compare to FDIC published state totals | Within 0.1% | 2026-08-14 | **PASS — exact** | All 14 state-years match to the digit on **both** deposits and branch counts. Zero discrepancy, not merely within tolerance. Figures below. The reference had to be corrected first — see notes. |
| UAT-02 | AC-02 | Count branches with null tract_geoid | Zero, or enumerated with reasons | 2026-08-14 | **PASS with exceptions** | 6,466 of 6,467 branches matched to exactly one tract; 1 has no coordinates. **But AC-02 is weaker than it looks** — 31 branches whose coordinates disagree with FDIC's reported county pass it cleanly. See `docs/spatial_join_exceptions.md`. |
| UAT-03 | AC-03 | Count tracts in >1 primary catchment | Zero | 2026-08-14 | **PASS** | 0, tested twice — in `08_catchments.py` and again in the warehouse against the loaded model. 1,849 covered tracts, each with exactly one primary. |
| UAT-04 | AC-04 | Clean re-run from raw; diff index outputs | Identical | | | Index not yet built. Warehouse is dropped and rebuilt on every run. |
| UAT-05 | AC-05 | Set each weight to zero in turn | Ranking changes each time | | | Index not yet built (script 10). |
| UAT-06 | FR-01 | Exercise every filter combination | No blank visuals or errors | | | Dashboard not yet built. |
| UAT-07 | FR-02 | Walk the full drill path | Context carries at each level | | | Dashboard not yet built. |
| UAT-08 | AC-06 | LMI coverage, current vs recommended | Both present and explained | | | Current footprint measured: 27.3% of catchment households are in LMI tracts vs 25.4% across WI+IL, +1.9pp. Recommended set awaits script 10. **31 tracts have no LMI basis and are enumerated.** |
| UAT-09 | FR-05 | Hand refresh procedure to another person | Completes without help | | | Not yet run. Needs a real person. |
| UAT-10 | Crosswalk | Subject resolves to one LEI in 2025 LAR | Exactly one match | 2026-08-14 | **PASS** | CERT 5296 → RSSD 917742 → LEI `ZF85QS7OXKPBG52R7N18`, 9,260 applications, exactly one certificate resolving to that LEI. Confirmed independently against GLEIF. |

---

## UAT-01 — the figures

Deposits in thousands of dollars, as reported by SOD. `computed` is our
warehouse; `published` is FDIC's server-side aggregate over the full source
table, fetched separately and SHA-256 pinned in the manifest.

| State | Year | Computed branches | Published branches | Computed deposits (k) | Published deposits (k) | Diff |
|---|---|---|---|---|---|---|
| IL | 2019 | 4,217 | 4,217 | 500,190,435 | 500,190,435 | 0 |
| IL | 2020 | 4,086 | 4,086 | 611,446,083 | 611,446,083 | 0 |
| IL | 2021 | 3,917 | 3,917 | 660,819,458 | 660,819,458 | 0 |
| IL | 2022 | 3,786 | 3,786 | 697,240,472 | 697,240,472 | 0 |
| IL | 2023 | 3,715 | 3,715 | 667,063,236 | 667,063,236 | 0 |
| IL | 2024 | 3,657 | 3,657 | 686,723,859 | 686,723,859 | 0 |
| IL | 2025 | 3,619 | 3,619 | 704,463,220 | 704,463,220 | 0 |
| WI | 2019 | 1,921 | 1,921 | 151,228,530 | 151,228,530 | 0 |
| WI | 2020 | 1,853 | 1,853 | 179,311,683 | 179,311,683 | 0 |
| WI | 2021 | 1,799 | 1,799 | 200,854,464 | 200,854,464 | 0 |
| WI | 2022 | 1,740 | 1,740 | 205,893,697 | 205,893,697 | 0 |
| WI | 2023 | 1,702 | 1,702 | 195,667,067 | 195,667,067 | 0 |
| WI | 2024 | 1,659 | 1,659 | 193,429,176 | 193,429,176 | 0 |
| WI | 2025 | 1,644 | 1,644 | 201,069,309 | 201,069,309 | 0 |

**What this validates.** The two figures reach the same number by entirely
different paths. FDIC computes theirs in their own engine over the full source
table. Ours travels API paging → CSV round-trip → staging → type coercion →
DuckDB load → thousands-to-dollars conversion → re-aggregation. Every step
could drop a row, truncate an identifier, or apply a unit conversion twice.
None did, in any of 14 state-years.

**The reference had to be corrected before the test was meaningful.** AC-01 as
written says "FDIC published state totals", which most naturally reads as the
`/banks/summary` endpoint. That endpoint is **not comparable**: it reports
deposits of institutions *headquartered* in a state, from Call Reports, while
SOD allocates deposits to the *branch's* location. For 2024 the two differ by
**50% in Wisconsin and 15% in Illinois**. Reconciling against it would have
reported a catastrophic failure that was really a mismatch between two
different populations — the same wrong-population error this project has now
found five times. The reference used is FDIC's SOD aggregate, which measures
what the warehouse measures.

**Reported by state and year deliberately**, never as one figure, so that the
shape of any future failure points at its cause: uniform and small suggests
units or rounding, concentrated in one state suggests scope, concentrated in
one year suggests vintage handling, a single state-year suggests a paging or
load fault.
