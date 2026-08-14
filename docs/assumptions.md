# Assumptions

Every judgment call, stated plainly. If a reviewer would ask "why did you do it
that way," the answer belongs here.

| ID | Assumption | Rationale | Risk if wrong |
|---|---|---|---|
| A-01 | Catchment = tracts whose centroid is within 3mi (urban) / 8mi (other) of a branch, straight-line | Drive-time modelling is out of scope; radius is what many retail teams use | Over/understates catchment in irregular geographies |
| A-02 | Deposits reported in SOD reflect the booking branch, not customer residence | This is how the FDIC survey works | Headquarters branches overstate local demand |
| A-03 | LMI is `tract_to_msa_income_percentage < 80`, taken directly from the HMDA file, which is the FFIEC's own tract-to-area income ratio | This is the regulatory definition and the exact figure examiners use, so it is not open to the objection that the analyst invented a threshold. 100% populated, covering 4,772 of 4,807 tracts. Falls back to ACS B19113 ÷ CBSA B19113 for the 35 tracts with no lending activity | A reconstruction from ACS would look defensible but differ from the official figure in edge cases. Using the published ratio removes that argument entirely. The 35 fallback tracts must be flagged as a different basis, not silently mixed |
| A-04 | Tracts outside any CBSA are treated as non-urban (8mi radius) | `config/catchment.yaml` keys the radius on CBSA membership; rural WI and IL tracts fall outside every CBSA | Rural tracts get the wider radius, which is probably right but is untested. Set empirically from observed branch spacing in Week 1 per `09` §1 rather than accepting the default |
| A-05 | The 2023 OMB CBSA delineation applies to all seven SOD vintages (2019–2025) | One delineation vintage pinned for consistency; 2025 does not exist and mixing vintages silently changes county→CBSA membership mid-series | Counties whose CBSA status changed in the 2023 revision are classified on the later basis for earlier years |
| A-06 | Branch identity is `UNINUMBR` (FDIC's unique office number), not the per-institution branch number | Confirmed present in the SOD API. The "branch number" named in 08 §6 is unique only within an institution and fails as a PK across the footprint | A per-institution branch number as PK would silently collide across institutions and corrupt every branch-level join |
