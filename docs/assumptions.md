# Assumptions

Every judgment call, stated plainly. If a reviewer would ask "why did you do it
that way," the answer belongs here.

| ID | Assumption | Rationale | Risk if wrong |
|---|---|---|---|
| A-01 | Catchment = tracts whose centroid is within 3mi (urban) / 8mi (other) of a branch, straight-line | Drive-time modelling is out of scope; radius is what many retail teams use | Over/understates catchment in irregular geographies |
| A-02 | Deposits reported in SOD reflect the booking branch, not customer residence | This is how the FDIC survey works | Headquarters branches overstate local demand |
| A-03 | LMI is defined as tract median **family** income (B19113) below 80% of CBSA median family income | This is the FFIEC/CRA definition. The original pull carried only median *household* income (B19013), which is a different and non-standard basis for the flag | A household-income basis would produce a defensible-looking but non-standard LMI flag that a compliance reviewer would reject. Both variables are now downloaded so the choice can be shown |
| A-04 | Tracts outside any CBSA have no area median to compare against and are treated as non-urban (8mi radius) | `config/catchment.yaml` keys the radius on CBSA membership; rural WI and IL tracts fall outside every CBSA | Rural tracts get no LMI flag under the CBSA basis. The CRA fallback is the statewide non-metropolitan median — decide before script 05 |
| A-05 | The 2023 OMB CBSA delineation applies to all seven SOD vintages (2019–2025) | One delineation vintage pinned for consistency; 2025 does not exist and mixing vintages silently changes county→CBSA membership mid-series | Counties whose CBSA status changed in the 2023 revision are classified on the later basis for earlier years |
| A-06 | Branch identity is `UNINUMBR` (FDIC's unique office number), not the per-institution branch number | Confirmed present in the SOD API. The "branch number" named in 08 §6 is unique only within an institution and fails as a PK across the footprint | A per-institution branch number as PK would silently collide across institutions and corrupt every branch-level join |
