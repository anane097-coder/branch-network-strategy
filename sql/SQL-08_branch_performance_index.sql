-- SQL-08 — Branch performance index: actual deposits vs catchment-predicted
--
-- BQ-3: Which of our branches underperform relative to what their catchment
--       demographics predict?
--
-- Technique: CTE chain with division guarded against null and zero; the index
--            the review-candidate recommendation rests on.
--
-- ============================================================================
-- LEVEL, NOT TRAJECTORY -- AND THE TWO MUST NOT BE CONFLATED
-- ============================================================================
-- This index measures whether a branch holds the deposits its catchment
-- should support. SQL-04 measures whether a branch is growing faster or
-- slower than its market. They answer different questions and can disagree
-- completely: a branch shrinking 37% a year can still hold twice the deposits
-- its catchment predicts, and a fast-growing branch can still be far below
-- what its market supports.
--
-- "Worst-performing branch" is ambiguous between the two and must never be
-- written without saying which. The columns below carry BOTH, and a branch
-- poor on BOTH is the review candidate with the strongest case -- stated as
-- "poor level and poor trajectory", never collapsed into one composite rank.
-- A single blended score would hide exactly the distinction a retail director
-- needs to act on: a shrinking-but-strong branch needs a different
-- intervention from a stable-but-weak one.
--
-- ============================================================================
-- MAIN OFFICES ARE EXCLUDED FROM THE INDEX
-- ============================================================================
-- SOD books deposits where the account was OPENED. A head office carries
-- corporate and brokered balances with no relationship to its catchment's
-- demographics: Brown County holds 18.6% of its county's branches and 51.7%
-- of its deposits. Scored against local demand, a head office produces the
-- most extreme apparent over-performance in the network by construction.
--
-- This is the same circularity as the largest-deposit catchment tie-break --
-- an outcome variable contaminating the input to a question about outcomes.
-- Left in, the index rewards booking rather than local performance, and BQ-5
-- then recommends siting new branches where the head office already is.
-- Excluded and reported separately, never silently dropped.
--
-- ============================================================================
-- WHAT "PREDICTED" MEANS
-- ============================================================================
-- Deliberately simple: each branch is expected to hold deposits in proportion
-- to its share of total catchment potential across the network. This is a
-- normalisation, not a model -- there is no fitting, no coefficients, and no
-- machine learning anywhere in this project. The index is a ratio, and its
-- assumptions are legible in four lines of SQL rather than buried in weights.

WITH eligible AS (
    SELECT
        b.uninumbr,
        b.city,
        b.state,
        b.is_main_office,
        b.position_drift_miles,
        b.drift_kind,
        b.county_agrees
    FROM dim_branch b
    WHERE b.is_subject_bank
      AND b.last_year = (SELECT max(year) FROM fact_branch_deposits)
      AND b.tract_geoid IS NOT NULL
      AND coalesce(b.is_main_office, '0') <> '1'      -- see note above
      -- FULL-SERVICE BRANCHES ONLY (service types 11 and 12).
      -- Limited-service facilities -- drive-throughs, administrative offices,
      -- service types 21 and 23 -- structurally book NO deposits. Across all
      -- institutions in this footprint, 70.9% of type-23 and 85.7% of type-29
      -- branches report exactly zero, persistently in every vintage, while
      -- full-service branches report zero 0.6% of the time.
      --
      -- Scored in a deposit index they return 0.0000 and sort to the very top
      -- of the review-candidate list. The recommendation that follows is
      -- "close the drive-through because it does not take deposits" -- a
      -- structural fact read as catastrophic performance. Three of the
      -- subject's branches are affected.
      --
      -- Excluded here and reported separately, never silently dropped.
      AND b.service_type IN ('11', '12')
),

actual AS (
    SELECT f.uninumbr, f.deposits AS actual_deposits
    FROM fact_branch_deposits f
    WHERE f.year = (SELECT max(year) FROM fact_branch_deposits)
),

catchment AS (
    SELECT
        br.uninumbr,
        count(*)                                             AS tracts,
        count(*) FILTER (WHERE t.tract_status = 'suppressed') AS tracts_suppressed,
        sum(t.households) FILTER (WHERE t.tract_status = 'ok') AS households,
        -- Suppressed tracts contribute nothing rather than zero: they are
        -- excluded from the sum, and their count travels alongside so a
        -- thinly-measured catchment is visible.
        sum(t.households * t.median_family_income)
            FILTER (WHERE t.tract_status = 'ok')             AS potential
    FROM bridge_branch_catchment br
    JOIN dim_tract t USING (tract_geoid)
    WHERE br.is_primary
    GROUP BY br.uninumbr
),

-- Trajectory, carried through from the BQ-2 measure so the two can be read
-- together without being merged.
trajectory AS (
    SELECT
        uninumbr,
        100.0 * (power(deposits::DOUBLE / nullif(lag_dep, 0),
                       1.0 / nullif(year - lag_year, 0)) - 1) AS cagr_pct
    FROM (
        SELECT
            uninumbr, year, deposits,
            lag(deposits, 3) OVER (PARTITION BY uninumbr ORDER BY year) AS lag_dep,
            lag(year, 3)     OVER (PARTITION BY uninumbr ORDER BY year) AS lag_year
        FROM fact_branch_deposits
    )
    WHERE year = (SELECT max(year) FROM fact_branch_deposits)
      AND lag_dep > 0
),

-- ============================================================================
-- NORMALISE WITHIN MARKET, NOT ACROSS THE NETWORK
-- ============================================================================
-- A network-wide normalisation measures the wrong thing for a bank with
-- uneven market position. Associated holds $26.0bn across 137 Wisconsin
-- branches and $5.8bn across 30 Illinois branches, while Chicago tracts are
-- far denser -- so every Illinois branch scores below 1.0 and every Wisconsin
-- branch above it. Measured: median index 1.123 in WI against 0.630 in IL.
--
-- That is a statement about where the bank is dominant and where it is a
-- challenger. It is NOT a statement about whether a branch converts the
-- demand around it, which is what BQ-3 asks. Normalising within market makes
-- the comparison "is this branch weak FOR THIS MARKET" -- the question a
-- retail director can act on.
--
-- Same reasoning as SQL-04 ranking growth within CBSA rather than across the
-- footprint. The network-wide figure is retained as a secondary column, so
-- the market-position effect stays visible rather than being normalised away.
market AS (
    SELECT
        coalesce(t.cbsa, 'RURAL-' || e.state)          AS market,
        sum(c.potential)                               AS market_potential,
        sum(a.actual_deposits)                         AS market_deposits,
        count(*)                                       AS market_branch_count
    FROM eligible e
    JOIN catchment c USING (uninumbr)
    JOIN actual    a USING (uninumbr)
    JOIN dim_branch b USING (uninumbr)
    LEFT JOIN dim_tract t ON b.tract_geoid = t.tract_geoid
    GROUP BY 1
),

network AS (
    SELECT
        sum(c.potential)                AS total_potential,
        sum(a.actual_deposits)          AS total_deposits,
        median(a.actual_deposits)       AS median_branch_deposits
    FROM eligible e
    JOIN catchment c USING (uninumbr)
    JOIN actual    a USING (uninumbr)
),

indexed AS (
    SELECT
        e.uninumbr,
        e.city,
        e.state,
        e.position_drift_miles,
        e.drift_kind,
        e.county_agrees,
        c.tracts,
        c.tracts_suppressed,
        c.households,
        c.potential,
        a.actual_deposits,
        m.market,
        m.market_branch_count,
        -- PRIMARY: this branch's share of its MARKET's potential, applied to
        -- that market's deposits.
        m.market_deposits * (c.potential::DOUBLE / nullif(m.market_potential, 0))
                                                        AS predicted_deposits,
        -- SECONDARY: the network-wide figure, kept so the market-position
        -- effect stays visible instead of being normalised out of sight.
        n.total_deposits * (c.potential::DOUBLE / nullif(n.total_potential, 0))
                                                        AS predicted_network,
        n.median_branch_deposits,
        t.cagr_pct
    FROM eligible e
    JOIN catchment c USING (uninumbr)
    JOIN actual    a USING (uninumbr)
    JOIN dim_branch b USING (uninumbr)
    LEFT JOIN dim_tract dt ON b.tract_geoid = dt.tract_geoid
    JOIN market m ON m.market = coalesce(dt.cbsa, 'RURAL-' || e.state)
    LEFT JOIN trajectory t USING (uninumbr)
    CROSS JOIN network n
)

SELECT
    uninumbr,
    city,
    state,
    tracts,
    tracts_suppressed,
    households,
    actual_deposits,
    round(predicted_deposits, 0)                            AS predicted_deposits,
    -- The index. Below 1.0: the branch holds less than its catchment supports.
    round(actual_deposits / nullif(predicted_deposits, 0), 4)
                                                            AS performance_index,
    round(cagr_pct, 2)                                      AS cagr_3y_pct,
    -- LEVEL and TRAJECTORY reported side by side, never blended.
    -- An unknown trajectory is its own state. A branch too new to have three
    -- years of history has no CAGR, and `cagr_pct < 0` is FALSE for NULL --
    -- so without this case it would be reported as having a POSITIVE
    -- trajectory it was never measured to have. Unknown is not a value.
    CASE
        WHEN cagr_pct IS NULL
             AND actual_deposits / nullif(predicted_deposits, 0) < 1
                              THEN 'poor level, trajectory unknown'
        WHEN cagr_pct IS NULL THEN 'adequate level, trajectory unknown'
        WHEN actual_deposits / nullif(predicted_deposits, 0) < 1
             AND cagr_pct < 0 THEN 'poor level AND poor trajectory'
        WHEN actual_deposits / nullif(predicted_deposits, 0) < 1
                              THEN 'poor level, positive trajectory'
        WHEN cagr_pct < 0     THEN 'adequate level, poor trajectory'
        ELSE                       'adequate level and trajectory'
    END                                                     AS diagnosis,
    market,
    market_branch_count,
    round(actual_deposits / nullif(predicted_network, 0), 4)
                                                            AS index_vs_network,
    -- Caveats that travel with the row rather than sitting in a footnote.
    (tracts_suppressed > 0)                                 AS catchment_partly_unmeasured,
    (drift_kind = 'relocation' AND position_drift_miles > 1) AS branch_relocated,
    (NOT county_agrees)                                     AS county_disputed,
    -- BOOKING CONCENTRATION. is_main_office flags one head office; it does
    -- not flag downtown commercial centres that book corporate balances the
    -- same way. Milwaukee's 250 E Wisconsin Ave holds $4.1bn -- 13% of the
    -- entire bank -- against a 4,282-household catchment, and is not a main
    -- office. Flagged rather than excluded: the deposits are real, they are
    -- simply not evidence that the surrounding demographics produced them.
    -- A branch above 20x the network median is doing something other than
    -- serving its catchment.
    (actual_deposits > 20 * median_branch_deposits)         AS booking_concentration
FROM indexed
ORDER BY performance_index;
