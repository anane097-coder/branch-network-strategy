-- SQL-03 — Branch deposit CAGR over three years
--
-- BQ-2: Where is our deposit share declining fastest, and is that a
--       market-wide trend or specific to our branches?
--
-- Technique: LAG over a year-ordered window; the growth base SQL-05's
--            attribution subtracts from.
--
-- LAG ASSUMES THE PREVIOUS ROW IS THE PREVIOUS YEAR. IT IS NOT ALWAYS.
-- A branch absent from a vintage -- closed and reopened, or missing from one
-- filing -- puts a non-adjacent year in the LAG slot, and the CAGR is then
-- computed over the wrong interval while looking perfectly ordinary.
-- prior_year is carried explicitly and years_elapsed derived from it, so the
-- exponent matches the actual gap rather than an assumed three.
--
-- Deposits are WHOLE DOLLARS (converted once, at load).

WITH branch_year AS (
    SELECT
        f.uninumbr,
        f.cert,
        b.state,
        substr(b.tract_geoid, 1, 5)  AS county_fips,
        b.is_subject_bank,
        b.is_main_office,
        f.year,
        f.deposits
    FROM fact_branch_deposits f
    JOIN dim_branch b USING (uninumbr)
    WHERE b.tract_geoid IS NOT NULL
),

with_lag AS (
    SELECT
        *,
        lag(deposits, 3) OVER w  AS deposits_3y_ago,
        lag(year, 3)     OVER w  AS prior_year
    FROM branch_year
    WINDOW w AS (PARTITION BY uninumbr ORDER BY year)
),

cagr AS (
    SELECT
        *,
        year - prior_year AS years_elapsed
    FROM with_lag
    WHERE deposits_3y_ago IS NOT NULL
      AND deposits_3y_ago > 0
)

SELECT
    uninumbr,
    cert,
    state,
    county_fips,
    is_subject_bank,
    is_main_office,
    prior_year,
    year,
    years_elapsed,
    deposits_3y_ago,
    deposits,
    -- Exponent uses the observed gap, not a hardcoded 3. A branch missing
    -- from a vintage would otherwise have its growth annualised over the
    -- wrong period.
    round(100.0 * (power(deposits::DOUBLE / deposits_3y_ago,
                         1.0 / nullif(years_elapsed, 0)) - 1), 3) AS cagr_pct,
    -- A gap other than 3 means the branch was absent from a vintage. Kept
    -- rather than filtered, because a branch that vanishes and returns is a
    -- finding about the filing, not a nuisance row.
    (years_elapsed <> 3) AS irregular_interval
FROM cagr
ORDER BY is_subject_bank DESC, cagr_pct;
