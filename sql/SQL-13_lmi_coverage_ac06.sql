-- SQL-13 — LMI coverage, current footprint against recommended
--
-- BQ-6: Does the recommended expansion improve or worsen access for low- and
--       moderate-income communities?
--
-- Technique: coverage as a household share over a set-membership join, with
--            the criterion as written reported beside the test that can fail.
--
-- ============================================================================
-- AC-06 AS WRITTEN CANNOT FAIL. IT IS REPORTED ANYWAY, AND SEPARATELY.
-- ============================================================================
-- "LMI coverage rate is computed for both current and recommended footprints"
-- with UAT-08 asking that "both values present and explained" is a REPORTING
-- criterion. It passes whenever two numbers exist. A recommendation that
-- widened the gap in every respect would satisfy it in full.
--
-- The obvious stronger reading - coverage must not fall - ALSO cannot fail,
-- structurally: a new branch adds catchment area and removes none, so coverage
-- after expansion is monotonically at least coverage before it. No possible
-- recommendation can violate it.
--
-- So AC-06 is discharged here as the deliverable it is, and the binding test
-- below is reported next to it. The two must not be confused: one is evidence
-- about equity, the other is evidence that two numbers were computed.
--
-- ============================================================================
-- THE BINDING TEST: PROPORTIONALITY, NOT IMPROVEMENT
-- ============================================================================
--     delta_LMI  >=  delta_nonLMI
--
-- Expansion must extend coverage to LMI households at least as much as to
-- everyone else. Both deltas are non-negative by construction, so this asks
-- whether the gap NARROWS - a recommendation can raise LMI coverage in
-- absolute terms while widening it. Pre-registered in
-- docs/siting_decision_rule.md, committed before any footprint was computed.
--
-- ============================================================================
-- HOUSEHOLDS, NOT TRACTS. AND THE 31 NO-BASIS TRACTS LEAVE BOTH SIDES.
-- ============================================================================
-- The tract basis inflated the over-index from +1.9pp to +3.3pp; the household
-- basis is what an examiner reads. Tracts with no LMI determination by either
-- route are excluded from numerator AND denominator - counting them as
-- non-LMI would inflate the denominator and quietly improve every figure here,
-- which is the third time that exact substitution has been available in this
-- project and the third time it is refused.
--
-- Coverage sets come from scripts/12_siting_recommendation.py rather than
-- being recomputed. A second distance implementation in SQL would drift from
-- the projected one, and the drift would be silent because both would look
-- plausible. Columns are DECLARED, not sniffed.

WITH covered AS (
    SELECT * FROM read_csv(
        'data/staging/ref_recommended_coverage.csv',
        columns = {'rule': 'TEXT', 'tract_geoid': 'TEXT',
                   'newly_covered': 'BOOLEAN'},
        header = true)
),

-- The universe: every tract carrying an LMI determination and a household
-- count. Defined once, used by both footprints, so the denominator cannot
-- differ between them.
universe AS (
    SELECT tract_geoid, households, lmi_flag, lmi_basis
    FROM dim_tract
    WHERE lmi_flag IS NOT NULL AND households IS NOT NULL
),

totals AS (
    SELECT
        sum(households) FILTER (WHERE lmi_flag)       AS lmi_households,
        sum(households) FILTER (WHERE NOT lmi_flag)   AS non_lmi_households
    FROM universe
),

by_rule AS (
    SELECT
        c.rule,
        sum(u.households) FILTER (WHERE u.lmi_flag)     AS lmi_covered,
        sum(u.households) FILTER (WHERE NOT u.lmi_flag) AS non_lmi_covered,
        count(*)                                        AS tracts_covered
    FROM covered c
    JOIN universe u USING (tract_geoid)
    GROUP BY c.rule
),

rates AS (
    SELECT
        b.rule,
        b.tracts_covered,
        round(100.0 * b.lmi_covered / t.lmi_households, 4)         AS lmi_coverage_pct,
        round(100.0 * b.non_lmi_covered / t.non_lmi_households, 4) AS non_lmi_coverage_pct,
        round(100.0 * b.lmi_covered / t.lmi_households
            - 100.0 * b.non_lmi_covered / t.non_lmi_households, 4) AS coverage_gap_pp
    FROM by_rule b CROSS JOIN totals t
)

SELECT
    r.rule,
    r.tracts_covered,
    r.lmi_coverage_pct,
    r.non_lmi_coverage_pct,
    r.coverage_gap_pp,

    -- AC-06 AS WRITTEN: both values present. Always PASS by construction, and
    -- labelled so nobody mistakes it for an equity result.
    CASE WHEN r.lmi_coverage_pct IS NOT NULL
         THEN 'PASS - value present (criterion cannot fail)' END
                                                    AS ac06_as_written,

    -- The deltas against the current footprint, and the test that can fail.
    round(r.lmi_coverage_pct - cur.lmi_coverage_pct, 4)         AS delta_lmi_pp,
    round(r.non_lmi_coverage_pct - cur.non_lmi_coverage_pct, 4) AS delta_non_lmi_pp,
    CASE
        WHEN r.rule = 'current' THEN NULL
        -- NULL case written before the comparison. A missing delta must not
        -- fall through into PASS, which is exactly how a branch with no CAGR
        -- was once reported as having a positive trajectory.
        WHEN r.lmi_coverage_pct IS NULL OR cur.lmi_coverage_pct IS NULL
            THEN 'NOT MEASURED'
        WHEN (r.lmi_coverage_pct - cur.lmi_coverage_pct)
           >= (r.non_lmi_coverage_pct - cur.non_lmi_coverage_pct)
            THEN 'PASS - expansion is proportional'
        ELSE 'FAIL - non-LMI coverage grows faster'
    END                                             AS binding_test,
    -- How lopsided, expressed as a ratio a reader can quote.
    CASE WHEN r.rule <> 'current'
          AND (r.lmi_coverage_pct - cur.lmi_coverage_pct) > 0
         THEN round((r.non_lmi_coverage_pct - cur.non_lmi_coverage_pct)
                  / (r.lmi_coverage_pct - cur.lmi_coverage_pct), 2)
    END                                             AS non_lmi_growth_multiple
FROM rates r
CROSS JOIN (SELECT * FROM rates WHERE rule = 'current') cur
ORDER BY CASE r.rule WHEN 'current' THEN 0
                     WHEN 'B_constrained' THEN 1 ELSE 2 END;
