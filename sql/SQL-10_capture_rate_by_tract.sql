-- SQL-10 — The subject's share of tract originations against all lenders
--
-- BQ-4: What mortgage demand exists in our footprint that we are not
--       capturing?
--
-- Technique: share calculation against an all-lender denominator; the capture
--            rate that SQL-11 ranks unmet demand on.
--
-- ORIGINATIONS ONLY, action_taken = 1, ON BOTH SIDES OF THE RATIO.
-- A capture rate is meaningless unless numerator and denominator count the
-- same thing. Purchased loans (code 6) would inflate the denominator far more
-- than the numerator -- Associated is 0.8% purchased, several lenders here
-- exceed 88% -- and every tract would show artificially low capture.
--
-- THE DENOMINATOR NEEDS NO CROSSWALK, WHICH IS WHY BQ-4 SURVIVED IT.
-- All-lender originations come straight from the loan-level file by tract.
-- Only 306 of 699 institutions resolve CERT to LEI, but an unresolved lender
-- still counts toward the tract total -- it simply cannot be named. The
-- crosswalk is needed to identify WHICH lei is the subject, not to build the
-- market.
--
-- CAPTURE RATE IS NOT A PERFORMANCE MEASURE ON ITS OWN.
-- A 0% capture rate in a tract where the subject has no branch within 20 miles
-- is a fact about geography, not about the bank. is_in_catchment is carried so
-- the two cases stay distinguishable; SQL-11 uses it to separate "demand we
-- are missing where we operate" from "demand outside our footprint".

WITH tract_totals AS (
    SELECT
        tract_geoid,
        sum(record_count) FILTER (WHERE action_taken = '1')  AS all_originations,
        sum(total_amount) FILTER (WHERE action_taken = '1')  AS all_origination_amt,
        sum(record_count) FILTER (WHERE action_taken IN ('1','2','3','4','5'))
                                                             AS all_applications,
        count(DISTINCT lei) FILTER (WHERE action_taken = '1') AS lenders_originating
    FROM fact_tract_lending
    GROUP BY tract_geoid
),

subject_lending AS (
    SELECT
        f.tract_geoid,
        sum(f.record_count) FILTER (WHERE f.action_taken = '1') AS subject_originations,
        sum(f.total_amount) FILTER (WHERE f.action_taken = '1') AS subject_origination_amt,
        sum(f.record_count) FILTER (WHERE f.action_taken IN ('1','2','3','4','5'))
                                                                AS subject_applications
    FROM fact_tract_lending f
    JOIN dim_institution i ON i.lei = f.lei
    WHERE i.is_subject_bank
    GROUP BY f.tract_geoid
),

-- Does the subject operate anywhere near this tract? Any catchment, not just
-- the primary one -- a branch serves customers regardless of which branch
-- happens to be nearest.
footprint AS (
    SELECT DISTINCT tract_geoid, TRUE AS is_in_catchment
    FROM bridge_branch_catchment
)

SELECT
    tt.tract_geoid,
    d.county_name,
    d.tier,
    d.households,
    d.lmi_flag,
    d.lmi_basis,
    coalesce(f.is_in_catchment, FALSE)        AS is_in_catchment,
    tt.lenders_originating,
    tt.all_applications,
    tt.all_originations,
    tt.all_origination_amt,
    coalesce(s.subject_originations, 0)       AS subject_originations,
    coalesce(s.subject_origination_amt, 0)    AS subject_origination_amt,
    -- The capture rate. Zero where the subject originates nothing, which is a
    -- real value rather than a null: the tract has demand and the subject took
    -- none of it.
    round(100.0 * coalesce(s.subject_originations, 0)
        / nullif(tt.all_originations, 0), 3)  AS capture_rate_pct,
    -- Unmet demand in absolute terms: originations by everyone else.
    tt.all_originations - coalesce(s.subject_originations, 0)
                                              AS competitor_originations,
    tt.all_origination_amt - coalesce(s.subject_origination_amt, 0)
                                              AS competitor_origination_amt
FROM tract_totals tt
JOIN dim_tract d USING (tract_geoid)
LEFT JOIN subject_lending s USING (tract_geoid)
LEFT JOIN footprint f USING (tract_geoid)
WHERE tt.all_originations > 0
ORDER BY competitor_originations DESC;
