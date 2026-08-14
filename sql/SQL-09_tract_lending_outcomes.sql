-- SQL-09 — Origination and denial counts by tract and lender
--
-- BQ-4: What mortgage demand exists in our footprint that we are not
--       capturing?
--
-- Technique: conditional aggregation over an outcome code held in the grain;
--            the lending base SQL-10 and SQL-11 build on.
--
-- ============================================================================
-- ORIGINATIONS ARE action_taken = 1. NOTHING ELSE.
-- ============================================================================
-- This is why action_taken sits in fact_tract_lending's grain rather than
-- being pre-aggregated in staging: no query can count originations without
-- naming the code, so the choice is visible here where a reviewer reads it
-- instead of buried in a script they would have to trust.
--
--   1  originated       2  approved, not accepted   3  denied
--   4  withdrawn        5  closed incomplete        6  PURCHASED
--   7  preapproval denied                 8  preapproval approved not accepted
--
-- Code 6 is the dangerous one. A purchased loan was BOUGHT on the secondary
-- market and says nothing about whether that lender serves the tract.
-- Including it overstates lending by 18.9% overall -- and unevenly, which is
-- worse: Associated is 0.8% purchased while several lenders here exceed 88%,
-- one showing 1,252 purchased against 13 originated. Swept into a capture
-- rate it would inflate competitors far more than the subject and manufacture
-- unmet demand that does not exist.
--
-- Codes 7 and 8 are preapproval outcomes, a different universe from
-- applications, and are excluded from the demand denominator.
--
-- ============================================================================
-- A-07: TRACT-LEVEL DENIAL RATES READ LOW. STATED AT THE POINT OF COMPUTATION.
-- ============================================================================
-- denial_count below carries a KNOWN DOWNWARD BIAS. 1.9% of denials have no
-- census tract against 0.3% of originations -- denials are geographically
-- unattributable at roughly six times the rate, so any denial rate computed
-- at this grain understates.
--
-- The mechanism is ordinary: applications denied early, on incomplete files or
-- on credit before a property is identified, never acquire an address to
-- geocode. It is the same reason preapprovals dominate the untracted set.
--
-- This caveat is repeated in assumptions.md (A-07) and in the data dictionary
-- because a prose caveat separates from its number the moment a chart moves
-- into a deck. Denial-rate analysis at tract level sits directly adjacent to
-- fair-lending territory, where 05 s.4 is explicit that disparities are not
-- evidence of discrimination and that this file lacks the underwriting
-- factors that would explain them.

SELECT
    f.tract_geoid,
    f.lei,
    x.institution_name,
    x.cert,
    x.is_subject_bank,
    f.loan_purpose,

    -- Demand: every application the lender actually acted on. Codes 1-5.
    -- Excludes purchased loans (never applications to this lender) and
    -- preapprovals (a separate universe).
    sum(f.record_count) FILTER (WHERE f.action_taken IN ('1','2','3','4','5'))
                                                        AS application_count,

    -- Supply: originations only.
    sum(f.record_count) FILTER (WHERE f.action_taken = '1')
                                                        AS origination_count,
    sum(f.total_amount) FILTER (WHERE f.action_taken = '1')
                                                        AS origination_amount,

    -- Denials. Read with A-07 above: biased low at tract grain.
    sum(f.record_count) FILTER (WHERE f.action_taken = '3')
                                                        AS denial_count,

    -- Reported separately, never folded into the measures above.
    sum(f.record_count) FILTER (WHERE f.action_taken = '6')
                                                        AS purchased_count,
    sum(f.record_count) FILTER (WHERE f.action_taken IN ('7','8'))
                                                        AS preapproval_count,

    round(100.0 * sum(f.record_count) FILTER (WHERE f.action_taken = '1')
        / nullif(sum(f.record_count)
                 FILTER (WHERE f.action_taken IN ('1','2','3','4','5')), 0), 2)
                                                        AS origination_rate_pct,
    round(100.0 * sum(f.record_count) FILTER (WHERE f.action_taken = '3')
        / nullif(sum(f.record_count)
                 FILTER (WHERE f.action_taken IN ('1','2','3','4','5')), 0), 2)
                                                        AS denial_rate_pct_biased_low
FROM fact_tract_lending f
LEFT JOIN dim_institution x ON x.lei = f.lei
GROUP BY f.tract_geoid, f.lei, x.institution_name, x.cert, x.is_subject_bank,
         f.loan_purpose
HAVING sum(f.record_count) FILTER (WHERE f.action_taken IN ('1','2','3','4','5')) > 0
ORDER BY f.tract_geoid, origination_count DESC;
