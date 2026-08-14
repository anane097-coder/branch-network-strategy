-- SQL-11 — Unmet mortgage demand, ranked
--
-- BQ-4: What mortgage demand exists in our footprint that we are not
--       capturing?
-- BQ-5: Where would three new branches sit, and what is the fiscal case?
--
-- Technique: NOT EXISTS against the catchment bridge; separation of demand
--            inside the footprint from demand outside it.
--
-- ============================================================================
-- FRAMING: UNDERSERVED DEMAND THE BANK IS MISSING, NOT A TARGETING LIST
-- ============================================================================
-- 05 s.4 is explicit that this is the defensible framing and also the more
-- commercially interesting one. A market-opportunity score that systematically
-- deprioritises lower-income tracts is a redlining model regardless of intent
-- and regardless of whether race was an input. This query therefore carries
-- lmi_flag on every row so the equity composition of any shortlist is visible
-- at the point of selection rather than audited afterwards in BQ-6.
--
-- ============================================================================
-- EVERY KNOWN BIAS HERE PUSHES THE SAME WAY: UNMET DEMAND READS HIGH
-- ============================================================================
-- Unattributed lending makes a market look LESS served than it is, which
-- inflates apparent unmet demand -- and that is precisely the direction that
-- manufactures expansion candidates. Three separate sources, all one-way:
--
--   1. THREE LENDERS REPORT NO GEOGRAPHY AT ALL. 1,735 records across three
--      LEIs carry a null tract on 100% of their rows. Their lending is
--      invisible to every tract in this query, so wherever they operate, the
--      market appears thinner than it is.
--
--   2. 7,863 RECORDS (1.12%) HAVE NO CENSUS TRACT and cannot enter a
--      tract-grain fact. Only 907 carry even a county, so there is no coarser
--      geography to fall back on. 17.1% of them are originations.
--
--   3. ONE ORPHAN TRACT (01125010405, Alabama) is excluded by the join. Three
--      applications; immaterial, but it is unattributed lending too.
--
-- None of these is large. Their significance is that they all point the same
-- way, so they do not cancel: a shortlist built from this query is biased
-- toward recommending expansion. The counts are small enough that the ranking
-- is unlikely to change, and that judgement should be stated rather than
-- assumed.
--
-- ============================================================================
-- THE RANKING BASIS DETERMINES THE EQUITY COMPOSITION. MEASURED, NOT ASSUMED.
-- ============================================================================
-- Ranking on ABSOLUTE unmet originations produces a shortlist that is 2% LMI
-- against a 29.8% footprint baseline. That is not a rounding effect, it is a
-- fifteen-fold under-representation, and no component of the ranking mentions
-- income or race.
--
-- The mechanism is structural. Origination VOLUME tracks affluence and tract
-- size -- spearman +0.473 against median family income, +0.704 against
-- households -- and the median LMI tract records 41 originations against 87
-- in the median non-LMI tract. Absolute unmet demand is therefore mechanically
-- lower in LMI tracts however underserved they are.
--
--   LMI share of the top 50, by ranking basis:
--     absolute unmet originations   2.0%
--     uncaptured applications       4.0%
--     unmet per household           4.0%
--     unmet per APPLICATION        50.0%
--
-- This is the 05 s.4 redlining risk arriving through the back door: not by
-- weighting income, but by ranking on a volume measure correlated with it. A
-- market-opportunity score that systematically deprioritises lower-income
-- tracts is a redlining model regardless of intent and regardless of whether
-- race was an input.
--
-- NO SINGLE BASIS IS ADOPTED HERE. All four are emitted as columns and the
-- default ordering is absolute unmet demand ONLY because it is the most
-- commercially conventional -- which is exactly why it must not be used
-- silently. BQ-5 must state which basis its shortlist was drawn on, and BQ-6
-- must report the equity composition of that basis rather than of a different
-- one. Choosing the basis IS the recommendation.
--
-- unmet_per_application is the measure closest to the framing 05 s.4 mandates:
-- demonstrated demand -- people who applied -- that the subject did not serve.
-- It normalises out tract size and affluence, which is why it recovers LMI
-- representation. It should not be adopted on that basis alone: a measure
-- chosen because it improves the equity number is reverse-engineered, the same
-- error the catchment radius avoided. It should be adopted, or not, on whether
-- "share of demonstrated demand not served" is the right definition of unmet.
--
-- ----------------------------------------------------------------------------
-- THE TWO BASES ANSWER DIFFERENT QUESTIONS. THAT IS DECIDABLE, NOT JUST FLAGGED
-- ----------------------------------------------------------------------------
-- ABSOLUTE unmet demand asks: where are the most loans being written that we
-- are not writing? That is a VOLUME question, and it correctly favours large
-- affluent tracts, because that is where the volume is. Its skew is not a
-- defect of the measure -- it is the measure answering the question asked.
--
-- PER-APPLICATION unmet demand asks: where is our capture weakest relative to
-- demand already demonstrated? That is a PENETRATION question, and it is
-- size- and affluence-neutral by construction.
--
-- A bank siting branches for deposit growth wants the first. A bank asked by
-- examiners why its lending footprint looks the way it does wants the second.
--
-- BQ-5 IS A SITING QUESTION, so absolute is defensible as the default -- and
-- that makes BQ-6's job HARDER, not easier. A shortlist that is 2.0% LMI
-- against a 29.8% baseline must be reported as that number and ADDRESSED, not
-- noted in passing. Per 05 s.4, an opportunity model that systematically
-- deprioritises LMI tracts is a redlining model in substance whatever its
-- inputs were.
--
-- THE OUTPUT IS THEREFORE BOTH RANKINGS, PRESENTED AS A TENSION RATHER THAN
-- RESOLVED INTO ONE NUMBER: ranked on commercial opportunity the shortlist is
-- 2.0% LMI; ranked on demonstrated-demand penetration it is substantially
-- different; the recommendation states what a bank would do holding both. That
-- is also how the decision is actually made in a bank where a CRA officer
-- holds a veto -- which is the stakeholder structure 08 specifies.
--
-- ============================================================================
-- TWO DIFFERENT QUESTIONS, KEPT APART
-- ============================================================================
-- "Demand we are missing where we already operate" is a conversion problem.
-- "Demand outside our footprint entirely" is a siting problem. They need
-- different interventions and must not be pooled into one ranking --
-- opportunity_type below separates them.

WITH tract_lending AS (
    SELECT
        tract_geoid,
        sum(record_count) FILTER (WHERE action_taken = '1')  AS all_originations,
        sum(total_amount) FILTER (WHERE action_taken = '1')  AS all_origination_amt,
        sum(record_count) FILTER (WHERE action_taken IN ('1','2','3','4','5'))
                                                             AS all_applications
    FROM fact_tract_lending
    GROUP BY tract_geoid
),

subject_lending AS (
    SELECT
        f.tract_geoid,
        sum(f.record_count) FILTER (WHERE f.action_taken = '1') AS subject_originations
    FROM fact_tract_lending f
    JOIN dim_institution i ON i.lei = f.lei
    WHERE i.is_subject_bank
    GROUP BY f.tract_geoid
),

ranked AS (
    SELECT
        d.tract_geoid,
        d.county_name,
        d.state_fips,
        d.tier,
        d.households,
        d.median_family_income,
        d.lmi_flag,
        d.lmi_basis,
        d.tract_status,
        tl.all_applications,
        tl.all_originations,
        tl.all_origination_amt,
        coalesce(sl.subject_originations, 0) AS subject_originations,
        tl.all_originations - coalesce(sl.subject_originations, 0)
                                             AS unmet_originations,
        round(100.0 * coalesce(sl.subject_originations, 0)
            / nullif(tl.all_originations, 0), 3) AS capture_rate_pct,
        -- NOT EXISTS against the bridge: is this tract outside every catchment?
        NOT EXISTS (SELECT 1 FROM bridge_branch_catchment b
                    WHERE b.tract_geoid = d.tract_geoid) AS outside_footprint
    FROM (SELECT *, substr(tract_geoid, 1, 2) AS state_fips FROM dim_tract) d
    JOIN tract_lending tl USING (tract_geoid)
    LEFT JOIN subject_lending sl USING (tract_geoid)
    WHERE tl.all_originations > 0
)

SELECT
    tract_geoid,
    county_name,
    CASE state_fips WHEN '55' THEN 'WI' WHEN '17' THEN 'IL' ELSE state_fips END
                                                    AS state,
    tier,
    households,
    median_family_income,
    lmi_flag,
    lmi_basis,
    all_applications,
    all_originations,
    subject_originations,
    unmet_originations,
    all_origination_amt,
    capture_rate_pct,
    outside_footprint,

    -- FOUR RANKING BASES, EMITTED SIDE BY SIDE. See the header: the choice
    -- between them swings the LMI composition of a shortlist from 2% to 50%.
    unmet_originations                              AS basis_absolute,
    round(unmet_originations / nullif(households, 0), 5)
                                                    AS basis_per_household,
    round(unmet_originations / nullif(all_applications, 0), 5)
                                                    AS basis_per_application,
    round((1 - capture_rate_pct / 100.0) * all_applications, 2)
                                                    AS basis_uncaptured_apps,

    -- ASCII only in emitted DATA. Em-dashes are fine in these comments, but a
    -- value carrying one breaks on any cp1252 consumer -- Excel and Power BI
    -- among them -- and this column is a dashboard filter.
    CASE
        WHEN outside_footprint THEN 'siting - outside the footprint'
        WHEN capture_rate_pct = 0 THEN 'conversion - present, capturing none'
        ELSE 'conversion - present, under-capturing'
    END                                             AS opportunity_type,
    -- Carried so a shortlist's equity composition is visible while it is being
    -- built, not audited after the fact in BQ-6.
    (lmi_flag IS NULL)                              AS lmi_undetermined,
    (tract_status <> 'ok')                          AS tract_partly_unmeasured
FROM ranked
ORDER BY unmet_originations DESC;
