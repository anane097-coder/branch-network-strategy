-- SQL-07 — Catchment demographics aggregated per branch
--
-- BQ-3: Which of our branches underperform relative to what their catchment
--       demographics predict?
--
-- Technique: aggregation through the bridge table; the demand-side input to
--            the performance index in SQL-08.
--
-- PRIMARY CATCHMENT ONLY. A tract inside several catchments is counted once,
-- for the nearest branch. Summing over ALL catchment tracts would double-count
-- households across neighbouring branches and inflate every catchment in
-- dense markets -- 40.6% of covered tracts fall inside more than one
-- catchment, so this is not a rounding decision.
--
-- SUPPRESSED TRACTS ARE EXCLUDED FROM THE SUM, NOT TREATED AS ZERO.
-- 84 tracts have a Census-suppressed median family income. Letting them
-- contribute zero potential would understate a branch's catchment and make
-- the branch look like an over-performer against it -- the same
-- unknown-collapsed-to-a-default error that nearly inflated the BQ-6 equity
-- result. tracts_suppressed is reported per branch so a catchment measured on
-- thin data is visible rather than silently weaker.
--
-- THE BRIDGE IS SUBJECT-ONLY. bridge_branch_catchment covers Associated's
-- branches and no competitor's, by design. is_subject_bank is carried on every
-- bridge row; a query joining through it and expecting full coverage gets a
-- silently subject-only result.

WITH catchment_tracts AS (
    SELECT
        br.uninumbr,
        t.tract_geoid,
        t.households,
        t.median_family_income,
        t.median_hh_income,
        t.owner_occupied_units,
        t.median_home_value,
        t.lmi_flag,
        t.tract_status,
        br.distance_miles
    FROM bridge_branch_catchment br
    JOIN dim_tract t USING (tract_geoid)
    WHERE br.is_primary                     -- see note above
),

per_branch AS (
    SELECT
        uninumbr,
        count(*)                                            AS tracts,
        count(*) FILTER (WHERE tract_status = 'suppressed')  AS tracts_suppressed,
        count(*) FILTER (WHERE tract_status = 'ok')          AS tracts_measured,
        sum(households) FILTER (WHERE tract_status = 'ok')   AS households,
        sum(owner_occupied_units) FILTER (WHERE tract_status = 'ok')
                                                             AS owner_occupied,
        -- Catchment potential, per the design's KPI table: households times
        -- median income, summed over measured tracts only.
        sum(households * median_family_income)
            FILTER (WHERE tract_status = 'ok')               AS catchment_potential,
        -- Income weighted by households, so a small rich tract does not pull
        -- the catchment mean the way a plain average would.
        sum(households * median_family_income)
            FILTER (WHERE tract_status = 'ok')
          / nullif(sum(households) FILTER (WHERE tract_status = 'ok'), 0)
                                                             AS weighted_family_income,
        count(*) FILTER (WHERE lmi_flag)                     AS lmi_tracts,
        sum(households) FILTER (WHERE lmi_flag AND tract_status = 'ok')
                                                             AS lmi_households,
        round(avg(distance_miles), 2)                        AS mean_distance_miles,
        round(max(distance_miles), 2)                        AS max_distance_miles
    FROM catchment_tracts
    GROUP BY uninumbr
)

SELECT
    p.uninumbr,
    b.city,
    b.state,
    b.is_main_office,
    b.drift_kind,
    p.tracts,
    p.tracts_measured,
    p.tracts_suppressed,
    p.households,
    p.owner_occupied,
    p.catchment_potential,
    round(p.weighted_family_income, 0)                   AS weighted_family_income,
    p.lmi_tracts,
    p.lmi_households,
    -- LMI share of catchment households: the branch-level input to AC-06.
    -- Household basis, not tract counts -- LMI tracts are five times denser
    -- and five times smaller, so counting tracts overstates the share.
    round(100.0 * p.lmi_households / nullif(p.households, 0), 2)
                                                         AS lmi_household_share_pct,
    p.mean_distance_miles,
    p.max_distance_miles,
    -- A catchment where a meaningful share of tracts is unmeasured should not
    -- be compared to one where none are.
    round(100.0 * p.tracts_suppressed / nullif(p.tracts, 0), 1)
                                                         AS pct_tracts_suppressed
FROM per_branch p
JOIN dim_branch b USING (uninumbr)
ORDER BY p.catchment_potential DESC;
