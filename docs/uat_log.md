# UAT Log

Record every execution: passes, failures, and what fixed the failures.

**Do not sanitize this file.** A log showing only first-run passes reads as weak
testing. Failures found and resolved are the evidence that the testing was real.

| ID | Requirement | Method | Expected | Date | Result | Notes |
|---|---|---|---|---|---|---|
| UAT-01 | AC-01 | Run SQL-14; compare to FDIC published state totals | Within 0.1% | | | |
| UAT-02 | AC-02 | Count branches with null tract_geoid | Zero, or enumerated with reasons | | | |
| UAT-03 | AC-03 | Count tracts in >1 primary catchment | Zero | | | |
| UAT-04 | AC-04 | Clean re-run from raw; diff index outputs | Identical | | | |
| UAT-05 | AC-05 | Set each weight to zero in turn | Ranking changes each time | | | |
| UAT-06 | FR-01 | Exercise every filter combination | No blank visuals or errors | | | |
| UAT-07 | FR-02 | Walk the full drill path | Context carries at each level | | | |
| UAT-08 | AC-06 | LMI coverage, current vs recommended | Both present and explained | | | |
| UAT-09 | FR-05 | Hand refresh procedure to another person | Completes without help | | | |
| UAT-10 | Crosswalk | Subject resolves to one LEI in 2025 LAR | Exactly one match | | | |
