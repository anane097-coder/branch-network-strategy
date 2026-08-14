-- SQL-14 — Reconciliation: do our computed deposit totals match FDIC's?
--
-- AC-01: Branch deposits summed by state match FDIC published state totals
--        within 0.1%.
--
-- Technique: validation query. Full outer join against an external reference,
--            with the discrepancy decomposed rather than reported as a single
--            figure.
--
-- WHAT THIS RECONCILES AGAINST, AND WHY IT IS NOT CIRCULAR
-- --------------------------------------------------------
-- ref_sod_state_totals is FDIC's own server-side aggregate, computed by their
-- engine over the full SOD source table and fetched as a separate artifact
-- (data/raw/fdic_sod_state_totals.csv, SHA-256 pinned in the manifest).
--
-- Our figure travels a completely different path to the same number:
--   API paging -> CSV round-trip -> staging -> type coercion -> DuckDB load
--   -> thousands-to-dollars conversion -> this aggregation
-- Every one of those steps could drop a row, truncate an identifier, or apply
-- a unit conversion twice. Agreement means none of them did.
--
-- WHAT IT DELIBERATELY DOES *NOT* RECONCILE AGAINST
-- -------------------------------------------------
-- FDIC's /banks/summary endpoint also publishes a state DEP figure, and the
-- design doc's phrase "FDIC published state totals" most naturally reads as
-- that one. It is NOT comparable. /banks/summary reports deposits of
-- institutions HEADQUARTERED in a state, sourced from Call Reports; SOD
-- allocates deposits to the location of the BRANCH. For 2024 they differ by
-- 50% in Wisconsin and 15% in Illinois. Reconciling against it would report a
-- catastrophic failure that is really a definitional mismatch between two
-- different populations. See docs/data_quality_log.md.
--
-- READING A FAILURE
-- -----------------
-- The discrepancy is reported by state AND year, never as one number, because
-- the shape of a failure points at its cause:
--   * small and uniform across all state-years  -> units or rounding
--   * concentrated in one state                 -> scope or filter
--   * concentrated in one year                  -> vintage handling
--   * one state-year only                       -> a paging or load fault

WITH computed AS (
    SELECT
        b.state                              AS state,
        f.year                               AS year,
        count(*)                             AS branches,
        -- fact_branch_deposits is in WHOLE DOLLARS; the reference is in
        -- thousands. Converting here rather than storing thousands keeps the
        -- warehouse in one unit and puts the conversion where it is visible.
        sum(f.deposits) / 1000               AS deposits_thousands
    FROM fact_branch_deposits f
    JOIN dim_branch b USING (uninumbr)
    GROUP BY b.state, f.year
),

published AS (
    SELECT state, year, branches, deposits_thousands
    FROM ref_sod_state_totals
),

compared AS (
    SELECT
        coalesce(c.state, p.state)                    AS state,
        coalesce(c.year,  p.year)                     AS year,
        c.branches                                    AS computed_branches,
        p.branches                                    AS published_branches,
        c.deposits_thousands                          AS computed_deposits_k,
        p.deposits_thousands                          AS published_deposits_k,
        c.branches - p.branches                       AS branch_diff,
        c.deposits_thousands - p.deposits_thousands   AS deposit_diff_k,
        CASE
            WHEN p.deposits_thousands IS NULL OR p.deposits_thousands = 0
                THEN NULL
            ELSE 100.0 * (c.deposits_thousands - p.deposits_thousands)
                 / p.deposits_thousands
        END                                           AS deposit_diff_pct
    FROM computed c
    FULL OUTER JOIN published p
      ON c.state = p.state AND c.year = p.year
)

SELECT
    state,
    year,
    computed_branches,
    published_branches,
    branch_diff,
    computed_deposits_k,
    published_deposits_k,
    deposit_diff_k,
    round(deposit_diff_pct, 6)  AS deposit_diff_pct,
    CASE
        WHEN computed_branches IS NULL     THEN 'MISSING FROM WAREHOUSE'
        WHEN published_branches IS NULL    THEN 'MISSING FROM REFERENCE'
        WHEN branch_diff <> 0              THEN 'BRANCH COUNT MISMATCH'
        WHEN abs(deposit_diff_pct) > 0.1   THEN 'FAIL - outside 0.1%'
        WHEN deposit_diff_k <> 0           THEN 'PASS - within tolerance'
        ELSE 'PASS - exact'
    END                          AS ac01_result
FROM compared
ORDER BY state, year;
