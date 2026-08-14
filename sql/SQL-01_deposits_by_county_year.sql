-- SQL-01 — Deposits by county and year, all institutions
--
-- BQ-1: Which markets in our footprint have the strongest deposit opportunity
--       relative to competitor saturation?
--
-- Technique: aggregation with grouping sets; the market-size base that SQL-02
--            and the opportunity index both build on.
--
-- COUNTY COMES FROM THE COORDINATES, NOT FROM THE REPORTED FIELD.
-- dim_branch carries two county statements: county_fips as FDIC reports it,
-- and the first five digits of tract_geoid, which is where the branch's
-- coordinates actually fall. They disagree for 31 branches. AC-02's audit
-- resolved this in favour of the coordinates, because in every case checkable
-- by hand the branch's own address text agrees with them and not with the
-- reported county (an address reading "Chicago" reported in a county 190 miles
-- downstate). county_agrees is carried through so the affected rows stay
-- identifiable rather than being silently absorbed.
--
-- Deposits are WHOLE DOLLARS here. SOD reports thousands; the conversion
-- happens once, at load, in 10_build_warehouse.py.

WITH branch_county AS (
    SELECT
        b.uninumbr,
        b.cert,
        b.state,
        -- Geocoded county: the tract the coordinates fall in, first 5 digits.
        substr(b.tract_geoid, 1, 5)  AS county_fips,
        b.county_agrees,
        b.is_subject_bank
    FROM dim_branch b
    WHERE b.tract_geoid IS NOT NULL          -- 1 branch has no coordinates
),

deposits_by_county AS (
    SELECT
        bc.state,
        bc.county_fips,
        f.year,
        count(DISTINCT bc.uninumbr)                          AS branches,
        count(DISTINCT bc.cert)                              AS institutions,
        sum(f.deposits)                                      AS deposits,
        sum(f.deposits) FILTER (WHERE bc.is_subject_bank)    AS subject_deposits,
        count(DISTINCT bc.uninumbr) FILTER (WHERE bc.is_subject_bank)
                                                             AS subject_branches,
        -- Rows whose county attribution is contested, so a reader can judge
        -- whether a small county's figures rest on a disputed branch.
        count(DISTINCT bc.uninumbr) FILTER (WHERE NOT bc.county_agrees)
                                                             AS contested_branches
    FROM fact_branch_deposits f
    JOIN branch_county bc USING (uninumbr)
    GROUP BY bc.state, bc.county_fips, f.year
)

SELECT
    d.state,
    d.county_fips,
    t.county_name,
    d.year,
    d.institutions,
    d.branches,
    d.subject_branches,
    d.contested_branches,
    d.deposits,
    d.subject_deposits,
    -- Deposits per branch: the crude market-density measure the opportunity
    -- index refines. Competitor saturation proper needs households, which
    -- arrive via the catchment bridge rather than the county.
    round(d.deposits / nullif(d.branches, 0), 0)  AS deposits_per_branch
FROM deposits_by_county d
LEFT JOIN (
    -- One county name per county; dim_tract carries it per tract.
    SELECT DISTINCT county_fips, first_value(county_name) OVER (
        PARTITION BY county_fips ORDER BY county_name) AS county_name
    FROM dim_tract
    WHERE county_name IS NOT NULL
) t USING (county_fips)
ORDER BY d.state, d.county_fips, d.year;
