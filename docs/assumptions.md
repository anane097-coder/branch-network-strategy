# Assumptions

Every judgment call, stated plainly. If a reviewer would ask "why did you do it
that way," the answer belongs here.

| ID | Assumption | Rationale | Risk if wrong |
|---|---|---|---|
| A-01 | Catchment = tracts whose centroid is within 3mi (urban) / 8mi (other) of a branch, straight-line | Drive-time modelling is out of scope; radius is what many retail teams use | Over/understates catchment in irregular geographies |
| A-02 | Deposits reported in SOD reflect the booking branch, not customer residence | This is how the FDIC survey works | Headquarters branches overstate local demand |
| A-03 | | | |
