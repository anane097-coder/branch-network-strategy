# Data Dictionary

Populated as tables are built. Every field in the warehouse appears here.

## dim_branch

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| branch_id | TEXT | FDIC SOD | Unique branch identifier | |
| cert | TEXT | FDIC SOD | FDIC certificate number | TEXT - leading zeros matter |
| tract_geoid | TEXT | derived | 11-digit census tract | Spatial join, script 07 |

## dim_tract

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| tract_geoid | TEXT | ACS / TIGER | 11-digit GEOID | Primary key |
| lmi_flag | BOOLEAN | derived | Low/moderate income tract | Define threshold in assumptions.md |

## fact_branch_deposits

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| deposits | BIGINT | FDIC SOD | Branch deposits, whole dollars | SOD reports thousands - converted at staging |

## fact_tract_lending

| Field | Type | Source | Description | Notes |
|---|---|---|---|---|
| lei | TEXT | HMDA | Legal Entity Identifier | Bridges to cert via RSSD |
