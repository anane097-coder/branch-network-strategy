-- SQL-06 — Branch deposit percentile within its county
--
-- BQ-3: Which of our branches underperform relative to what their catchment
--       demographics predict?
--
-- Technique: PERCENT_RANK() OVER (PARTITION BY ...); the crude size-in-market
--            measure that SQL-08's demographic prediction improves on.
--
-- THIS MEASURES LEVEL, NOT TRAJECTORY. SQL-04 ranks branches by growth; this
-- ranks them by size. A branch can be large and shrinking, or small and
-- growing fast, and the two questions have different answers. "Worst
-- performing branch" is ambiguous between them and should never be written
-- without saying which. A branch poor on BOTH is the review candidate with
-- the strongest case, and SQL-08 joins the two explicitly.
--
-- Main offices are retained here but flagged. Unlike the performance index in
-- SQL-08, a percentile of raw deposits is not distorted by head-office
-- booking so much as dominated by it -- a head office IS genuinely the
-- largest branch in its county, which is a true statement about size and a
-- meaningless one about performance.

WITH branch_county AS (
    SELECT
        f.uninumbr,
        f.cert,
        b.state,
        substr(b.tract_geoid, 1, 5) AS county_fips,
        b.is_subject_bank,
        b.is_main_office,
        f.deposits
    FROM fact_branch_deposits f
    JOIN dim_branch b USING (uninumbr)
    WHERE f.year = (SELECT max(year) FROM fact_branch_deposits)
      AND b.tract_geoid IS NOT NULL
),

ranked AS (
    SELECT
        *,
        percent_rank() OVER (PARTITION BY county_fips ORDER BY deposits)
                                                     AS deposit_percentile,
        median(deposits) OVER (PARTITION BY county_fips)
                                                     AS county_median_deposits,
        count(*) OVER (PARTITION BY county_fips)     AS county_branches
    FROM branch_county
)

SELECT
    uninumbr,
    cert,
    state,
    county_fips,
    is_main_office,
    county_branches,
    deposits,
    county_median_deposits,
    -- The KPI from the design's table: branch deposits over the median branch
    -- in the same county. 1.0 means an exactly typical branch.
    round(deposits::DOUBLE / nullif(county_median_deposits, 0), 4)
                                                AS vs_county_median,
    round(deposit_percentile, 4)                AS deposit_percentile,
    -- A percentile over a handful of branches is arithmetically valid and
    -- analytically empty. Labelled rather than filtered.
    (county_branches < 10)                      AS thin_county
FROM ranked
WHERE is_subject_bank
ORDER BY deposit_percentile;
