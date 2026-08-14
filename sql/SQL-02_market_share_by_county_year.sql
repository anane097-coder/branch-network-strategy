-- SQL-02 — The subject's deposit market share by county and year
--
-- BQ-1: Which markets in our footprint have the strongest deposit opportunity
--       relative to competitor saturation?
--
-- Technique: aggregation with a subquery denominator; share computed against
--            the full market rather than against the subject's own total.
--
-- THE DENOMINATOR IS THE POINT OF THIS QUERY.
-- Market share is our deposits in a county divided by ALL institutions'
-- deposits in that county. That denominator only exists because the SOD
-- extract is filtered on STALPBR (branch location) rather than STALP (charter
-- state). Under the original filter every out-of-state-chartered competitor
-- was missing -- JPMorgan Chase alone has 263 Illinois branches -- and this
-- share would have been computed against a denominator missing most of the
-- market, reporting the subject as far larger than it is. See the quality log.
--
-- HEADQUARTERS BOOKING IS THE STANDING CAVEAT ON EVERY FIGURE HERE.
-- SOD allocates deposits to the branch where the account was OPENED, not
-- where the customer lives. Large corporate and brokered balances concentrate
-- at headquarters branches, so a county containing a bank's head office shows
-- deposits that have little to do with local demand. This is the project's
-- headline limitation and it bites hardest in exactly this query, because
-- share is a ratio of two figures both distorted the same way. The
-- hq_branches column below makes the affected counties identifiable.

WITH branch_county AS (
    SELECT
        b.uninumbr,
        b.cert,
        b.state,
        substr(b.tract_geoid, 1, 5)  AS county_fips,   -- geocoded, see SQL-01
        b.is_subject_bank,
        b.is_main_office
    FROM dim_branch b
    WHERE b.tract_geoid IS NOT NULL
),

county_market AS (
    SELECT
        bc.state,
        bc.county_fips,
        f.year,
        sum(f.deposits)                                   AS market_deposits,
        count(DISTINCT bc.cert)                           AS institutions,
        count(DISTINCT bc.uninumbr)                       AS market_branches,
        -- Main offices in the county: where the booking distortion lives.
        count(DISTINCT bc.uninumbr) FILTER (WHERE bc.is_main_office = '1')
                                                          AS hq_branches
    FROM fact_branch_deposits f
    JOIN branch_county bc USING (uninumbr)
    GROUP BY bc.state, bc.county_fips, f.year
),

subject_presence AS (
    SELECT
        bc.county_fips,
        f.year,
        sum(f.deposits)              AS subject_deposits,
        count(DISTINCT bc.uninumbr)  AS subject_branches
    FROM fact_branch_deposits f
    JOIN branch_county bc USING (uninumbr)
    WHERE bc.is_subject_bank
    GROUP BY bc.county_fips, f.year
),

share AS (
    SELECT
        m.state,
        m.county_fips,
        m.year,
        m.institutions,
        m.market_branches,
        m.hq_branches,
        m.market_deposits,
        coalesce(s.subject_deposits, 0)  AS subject_deposits,
        coalesce(s.subject_branches, 0)  AS subject_branches,
        -- Subject absent from a county is 0% share, not NULL: the market
        -- exists and the subject holds none of it, which is the finding.
        100.0 * coalesce(s.subject_deposits, 0)
              / nullif(m.market_deposits, 0)  AS share_pct,
        100.0 * coalesce(s.subject_branches, 0)
              / nullif(m.market_branches, 0)  AS branch_share_pct
    FROM county_market m
    LEFT JOIN subject_presence s
      ON m.county_fips = s.county_fips AND m.year = s.year
)

SELECT
    state,
    county_fips,
    year,
    institutions,
    market_branches,
    subject_branches,
    hq_branches,
    market_deposits,
    subject_deposits,
    round(share_pct, 3)         AS deposit_share_pct,
    round(branch_share_pct, 3)  AS branch_share_pct,
    -- Deposit share far above branch share means the subject's branches in
    -- this county are unusually large -- or that one of them books
    -- headquarters deposits. The gap is diagnostic, not a performance signal.
    round(share_pct - branch_share_pct, 3) AS share_gap_pp
FROM share
ORDER BY state, county_fips, year;
