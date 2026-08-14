-- SQL-05 — Market-versus-branch attribution
--
-- BQ-2: Where is our deposit share declining fastest, and is that a
--       market-wide trend or specific to our branches?
--
-- Technique: window functions over a CTE chain; the subject's growth net of
--            its market's growth, per county-year.
--
-- THIS IS THE QUERY BQ-2 ACTUALLY ASKS. Everything before it measures growth;
-- this one attributes it. A branch shrinking 8% in a market shrinking 10% is
-- gaining share. The same branch in a market growing 5% is losing badly. The
-- headline figure is excess_growth_pp -- subject growth minus market growth.
--
-- THE DENOMINATOR CAN MOVE FOR REASONS THAT ARE NOT ABOUT COMPETITION.
-- SOD books deposits where the account was opened, so a single institution
-- relocating a treasury relationship can swing a county's total without a
-- branch opening or closing anywhere. Measured in this data: McLean County IL
-- fell 73.2% in 2021 while losing 2 branches, and Knox County IL rose 69.2%
-- in 2022 while losing 1. Those are booking events, not market collapse or
-- boom, and attribution against them is meaningless.
--
-- Such county-years are FLAGGED, NOT DROPPED (market_volatile). Dropping them
-- would quietly remove counties from BQ-2 and the reader would never know a
-- market had been excluded; flagging lets the finding be discounted on sight.
--
-- The 2022->2023 discontinuity that earlier drafts of this project worried
-- about does NOT exist. It was an artifact of filtering SOD on STALP (charter
-- state) rather than STALPBR (branch location) -- BMO's Bank of the West
-- acquisition is a western-US event that barely touches this footprint. In the
-- corrected extract BMO goes 345 -> 346 branches across that transition. The
-- volatility above is real; the merger discontinuity was not.

WITH branch_county AS (
    SELECT
        f.uninumbr,
        b.state,
        substr(b.tract_geoid, 1, 5) AS county_fips,
        b.is_subject_bank,
        f.year,
        f.deposits
    FROM fact_branch_deposits f
    JOIN dim_branch b USING (uninumbr)
    WHERE b.tract_geoid IS NOT NULL
),

county_year AS (
    SELECT
        state,
        county_fips,
        year,
        sum(deposits)                                      AS market_deposits,
        count(DISTINCT uninumbr)                           AS market_branches,
        sum(deposits) FILTER (WHERE is_subject_bank)       AS subject_deposits,
        count(DISTINCT uninumbr) FILTER (WHERE is_subject_bank)
                                                           AS subject_branches
    FROM branch_county
    GROUP BY state, county_fips, year
),

with_prior AS (
    SELECT
        *,
        lag(market_deposits)  OVER w AS prev_market_deposits,
        lag(subject_deposits) OVER w AS prev_subject_deposits,
        lag(market_branches)  OVER w AS prev_market_branches,
        lag(subject_branches) OVER w AS prev_subject_branches,
        lag(year)             OVER w AS prev_year
    FROM county_year
    WINDOW w AS (PARTITION BY county_fips ORDER BY year)
),

growth AS (
    SELECT
        state,
        county_fips,
        prev_year,
        year,
        market_branches,
        subject_branches,
        market_deposits,
        subject_deposits,
        100.0 * (market_deposits - prev_market_deposits)
              / nullif(prev_market_deposits, 0)   AS market_growth_pct,
        100.0 * (subject_deposits - prev_subject_deposits)
              / nullif(prev_subject_deposits, 0)  AS subject_growth_pct,
        market_branches - prev_market_branches    AS market_branch_change,
        subject_branches - prev_subject_branches  AS subject_branch_change
    FROM with_prior
    WHERE prev_year IS NOT NULL
      AND year = prev_year + 1                    -- adjacent vintages only
)

SELECT
    state,
    county_fips,
    prev_year,
    year,
    market_branches,
    subject_branches,
    round(market_growth_pct, 2)   AS market_growth_pct,
    round(subject_growth_pct, 2)  AS subject_growth_pct,
    -- The BQ-2 answer. Positive: the subject gained share. Negative: it lost
    -- ground its market did not.
    round(subject_growth_pct - market_growth_pct, 2) AS excess_growth_pp,
    CASE
        WHEN subject_growth_pct IS NULL                    THEN 'no subject presence'
        WHEN subject_growth_pct > market_growth_pct        THEN 'gaining share'
        ELSE                                                    'losing share'
    END AS attribution,
    market_branch_change,
    subject_branch_change,
    -- A county whose deposits move more than 40% without a matching branch
    -- change is a booking event, not a market movement. Flagged, not dropped.
    (abs(market_growth_pct) > 40 AND abs(coalesce(market_branch_change, 0)) <= 2)
        AS market_volatile
FROM growth
WHERE subject_branches > 0
ORDER BY excess_growth_pp;
