-- SQL-04 — Rank branches by deposit growth within their CBSA
--
-- BQ-2: Where is our deposit share declining fastest, and is that a
--       market-wide trend or specific to our branches?
--
-- Technique: RANK() and PERCENT_RANK() OVER (PARTITION BY ...); ranking within
--            market rather than across the footprint.
--
-- WHY RANK WITHIN CBSA RATHER THAN ACROSS THE FOOTPRINT.
-- A branch in Chicago and a branch in rural Wisconsin do not compete, and a
-- footprint-wide ranking mostly sorts branches by how urban they are. Ranking
-- inside the market answers the question BQ-2 asks: is this branch losing
-- ground to its own competitors, or is the whole market moving?
--
-- CBSA comes from dim_tract via the branch's geocoded tract. Branches outside
-- any CBSA are ranked together in a 'RURAL-<state>' pseudo-market rather than
-- dropped -- 18 of the subject's branches sit outside every CBSA, and
-- excluding them would silently remove the rural network from BQ-2.
--
-- SMALL MARKETS ARE NOT SUPPRESSED, THEY ARE LABELLED. A percentile computed
-- over 3 branches is arithmetically valid and analytically meaningless.
-- market_branches is carried so a reader can discount it; filtering here
-- would hide markets rather than qualify them.

WITH branch_growth AS (
    SELECT
        f.uninumbr,
        f.cert,
        b.state,
        b.is_subject_bank,
        b.is_main_office,
        f.year,
        f.deposits,
        lag(f.deposits, 3) OVER (PARTITION BY f.uninumbr ORDER BY f.year)
                                                      AS deposits_3y_ago,
        lag(f.year, 3)     OVER (PARTITION BY f.uninumbr ORDER BY f.year)
                                                      AS prior_year,
        -- Market identity: the CBSA the branch's tract belongs to.
        coalesce(t.cbsa, 'RURAL-' || b.state)         AS market,
        coalesce(t.cbsa_title, 'Outside any CBSA, ' || b.state)
                                                      AS market_name
    FROM fact_branch_deposits f
    JOIN dim_branch b USING (uninumbr)
    LEFT JOIN dim_tract t ON b.tract_geoid = t.tract_geoid
    WHERE b.tract_geoid IS NOT NULL
),

latest AS (
    SELECT
        *,
        year - prior_year AS years_elapsed,
        100.0 * (power(deposits::DOUBLE / nullif(deposits_3y_ago, 0),
                       1.0 / nullif(year - prior_year, 0)) - 1) AS cagr_pct
    FROM branch_growth
    WHERE year = (SELECT max(year) FROM fact_branch_deposits)
      AND deposits_3y_ago > 0
),

ranked AS (
    SELECT
        *,
        rank()          OVER (PARTITION BY market ORDER BY cagr_pct DESC)
                                                      AS growth_rank_in_market,
        percent_rank()  OVER (PARTITION BY market ORDER BY cagr_pct)
                                                      AS growth_percentile,
        count(*)        OVER (PARTITION BY market)    AS market_branches,
        median(cagr_pct) OVER (PARTITION BY market)   AS market_median_cagr
    FROM latest
)

SELECT
    market,
    market_name,
    market_branches,
    uninumbr,
    cert,
    is_subject_bank,
    is_main_office,
    round(cagr_pct, 3)                              AS cagr_pct,
    round(market_median_cagr, 3)                    AS market_median_cagr,
    -- The BQ-2 answer in one column: positive means the branch beat its own
    -- market, negative means it lost ground the market did not.
    round(cagr_pct - market_median_cagr, 3)         AS vs_market_pp,
    growth_rank_in_market,
    round(growth_percentile, 4)                     AS growth_percentile,
    (market_branches < 10)                          AS thin_market
FROM ranked
WHERE is_subject_bank                -- competitors set the benchmark above
ORDER BY vs_market_pp;
