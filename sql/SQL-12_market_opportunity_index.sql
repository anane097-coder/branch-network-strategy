-- SQL-12 — The market opportunity index
--
-- BQ-5: Where would three new branches sit, and what is the fiscal case?
--
-- Technique: z-score normalisation across five components, weighted from a
--            reference table rather than from literals, with the per-component
--            scores retained so script 11 can attribute rank movement.
--
-- ============================================================================
-- WEIGHTS ARE JOINED, NOT WRITTEN. NORMALISATION IS z_score, NOT min_max.
-- ============================================================================
-- `ref_index_weights` is loaded from config/index_weights.yaml by script 10.
-- FR-03 requires weights adjustable without rebuilding the model, and a
-- literal 0.25 in this file would break that silently - worse, it would let
-- the config and the query disagree with nothing able to detect it.
--
-- min_max was abandoned because it anchors the whole scale on the two most
-- extreme tracts, and at the top end that extreme is not a measurement:
-- 17 tracts report median_hh_income of exactly $250,001, the ACS TOP-CODE, a
-- censored bound meaning "at least". A top-code is a default standing in for a
-- state at the measurement level rather than the data level, which is why no
-- null check reaches it - the value is present, real, and does not mean what
-- it appears to. z_score does not anchor on extremes.
--
-- ============================================================================
-- FOUR OF THE FIVE COMPONENTS WERE REFORMULATED BEFORE REACHING THIS QUERY
-- ============================================================================
-- A composite inherits every component's scale relationship at once, and the
-- weight-sensitivity analysis cannot surface that: varying weights across five
-- biased components yields five biased rankings. So each was tested against
-- tract size first (audit doc, "The scale test, run").
--
--   competitor_saturation  GRAIN MISMATCH, now fixed. Branches were counted
--                          over the catchment and divided by households in the
--                          TRACT. Pooled correlation -0.404 against tract size,
--                          a sevenfold gradient across household deciles. With
--                          the denominator matched to the numerator: +0.064.
--                          Ranked tract size, not saturation.
--
--   deposit_market_growth  RETAIL basis. County CAGR on all deposits is
--                          dominated by booking centres - McLean IL read
--                          -16.9% because State Farm Bank held $11.39bn across
--                          two branches in 2020 and was gone by 2021, and
--                          Brown WI read +12.3% on the SUBJECT'S OWN HQ
--                          booking, which would have rewarded siting in the
--                          bank's home county because the bank books there.
--
--   household_growth       Built from non-overlapping ACS vintages across the
--                          2010/2020 tract boundary change. 12% of tracts are
--                          measured on a small overlap cluster rather than the
--                          tract, because their own apportioned value was
--                          uniform-density guesswork - one read +117% a year
--                          off a base of 34 households.
--
--   median_income          Not rescaled; the exposure was the normalisation,
--                          handled above.
--
--   unmet_mortgage_demand  DELIBERATELY NOT ADJUSTED. It carries the strongest
--                          size correlation here (+0.707) and that is the
--                          measure answering the question asked: under the
--                          absolute basis this is a VOLUME question, where a
--                          larger tract genuinely does hold more unmet demand.
--                          Adjusting it would delete signal to satisfy a test.
--                          It is also the component carrying the equity
--                          consequence measured in SQL-11 - a top-50 that is
--                          2.0% LMI against a 29.8% baseline - so if the
--                          ranking proves sensitive to any single component,
--                          this is the one where that matters most.
--
-- ============================================================================
-- NETWORK-WIDE NORMALISATION IS THE INTENT HERE, UNLIKE SQL-08
-- ============================================================================
-- The performance index normalises WITHIN market, because asking whether a
-- branch beats its own market is a different question from asking whether it
-- beats the network. This index does the opposite deliberately: BR-01 ranks
-- all markets in the footprint against each other, so a rural tract's
-- saturation SHOULD be comparable to a metro tract's. Stated because the two
-- queries look similar and mean opposite things.
--
-- ============================================================================
-- A TRACT MISSING ANY COMPONENT IS REFUSED, NOT PARTIALLY SCORED
-- ============================================================================
-- Renormalising the weights over whatever components happen to be present
-- would produce a score for every tract and quietly compare tracts measured on
-- five things against tracts measured on three. components_present is carried
-- so the refusal is countable rather than a silent absence.

WITH lending AS (
    SELECT
        f.tract_geoid,
        sum(f.record_count) FILTER (WHERE f.action_taken = '1') AS all_originations,
        sum(f.record_count) FILTER (WHERE f.action_taken = '1'
            AND i.is_subject_bank)                              AS subject_originations
    FROM fact_tract_lending f
    LEFT JOIN dim_institution i ON i.lei = f.lei
    GROUP BY 1
),

raw AS (
    SELECT
        t.tract_geoid,
        t.county_name,
        t.cbsa_title,
        t.tier,
        t.households,
        t.lmi_flag,
        t.tract_status,
        t.growth_basis,
        g.retail_basis_status,
        g.excluded_deposit_share,
        -- The five, each on the basis the scale test settled.
        t.household_growth_pct                          AS household_growth,
        t.median_hh_income                              AS median_income,
        g.cagr_pct_retail                               AS deposit_market_growth,
        c.competitor_per_10k_catchment_hh               AS competitor_saturation,
        -- Absolute unmet originations. NULL where the tract has no lending at
        -- all: that is an absence of evidence, not zero unmet demand, and
        -- zero would rank it as fully served.
        CASE WHEN l.all_originations > 0
             THEN l.all_originations - coalesce(l.subject_originations, 0)
        END                                             AS unmet_mortgage_demand
    FROM dim_tract t
    LEFT JOIN fact_county_deposit_growth g ON g.county_fips = t.county_fips
    LEFT JOIN fact_tract_competition c     ON c.tract_geoid = t.tract_geoid
    LEFT JOIN lending l                    ON l.tract_geoid = t.tract_geoid
),

-- z = (x - mean) / sd, computed over the tracts where the component exists.
-- avg() and stddev_samp() both ignore NULLs, so a missing value does not drag
-- the mean toward zero - it simply does not participate.
scored AS (
    SELECT
        r.*,
        (household_growth - avg(household_growth) OVER ())
            / nullif(stddev_samp(household_growth) OVER (), 0)  AS z_household_growth,
        (median_income - avg(median_income) OVER ())
            / nullif(stddev_samp(median_income) OVER (), 0)     AS z_median_income,
        (deposit_market_growth - avg(deposit_market_growth) OVER ())
            / nullif(stddev_samp(deposit_market_growth) OVER (), 0)
                                                                AS z_deposit_market_growth,
        -- INVERTED: lower saturation is better, so the z-score is negated
        -- rather than the input, which keeps the raw column readable.
        -1 * (competitor_saturation - avg(competitor_saturation) OVER ())
            / nullif(stddev_samp(competitor_saturation) OVER (), 0)
                                                                AS z_competitor_saturation,
        (unmet_mortgage_demand - avg(unmet_mortgage_demand) OVER ())
            / nullif(stddev_samp(unmet_mortgage_demand) OVER (), 0)
                                                                AS z_unmet_mortgage_demand
    FROM raw r
),

w AS (
    SELECT component, weight
    FROM ref_index_weights
    WHERE scenario = 'primary'
),

weighted AS (
    SELECT
        s.*,
        (SELECT weight FROM w WHERE component = 'household_growth')      AS w_hg,
        (SELECT weight FROM w WHERE component = 'median_income')         AS w_mi,
        (SELECT weight FROM w WHERE component = 'deposit_market_growth') AS w_dg,
        (SELECT weight FROM w WHERE component = 'competitor_saturation') AS w_cs,
        (SELECT weight FROM w WHERE component = 'unmet_mortgage_demand') AS w_um
    FROM scored s
)

SELECT
    tract_geoid,
    county_name,
    cbsa_title,
    tier,
    households,
    lmi_flag,

    -- Raw component values, so a reader can see what produced the score.
    round(household_growth, 3)          AS household_growth,
    median_income,
    round(deposit_market_growth, 3)     AS deposit_market_growth,
    round(competitor_saturation, 3)     AS competitor_saturation,
    unmet_mortgage_demand,

    -- Per-component contributions. Script 11 attributes rank movement through
    -- these: a global stability figure cannot say WHICH component the answer
    -- hangs on, and with four of five reformulated at this stage, that is the
    -- more useful question.
    round(w_hg * z_household_growth, 4)         AS contrib_household_growth,
    round(w_mi * z_median_income, 4)            AS contrib_median_income,
    round(w_dg * z_deposit_market_growth, 4)    AS contrib_deposit_market_growth,
    round(w_cs * z_competitor_saturation, 4)    AS contrib_competitor_saturation,
    round(w_um * z_unmet_mortgage_demand, 4)    AS contrib_unmet_mortgage_demand,

    (CASE WHEN z_household_growth      IS NOT NULL THEN 1 ELSE 0 END
   + CASE WHEN z_median_income         IS NOT NULL THEN 1 ELSE 0 END
   + CASE WHEN z_deposit_market_growth IS NOT NULL THEN 1 ELSE 0 END
   + CASE WHEN z_competitor_saturation IS NOT NULL THEN 1 ELSE 0 END
   + CASE WHEN z_unmet_mortgage_demand IS NOT NULL THEN 1 ELSE 0 END)
                                                AS components_present,

    -- NULL unless all five are present. The addition would produce NULL on its
    -- own, but stating the condition means a reader does not have to know that
    -- to trust it - and it is the same three-valued-logic reliance that put a
    -- branch with no CAGR into the "positive trajectory" bucket.
    CASE WHEN z_household_growth      IS NOT NULL
          AND z_median_income         IS NOT NULL
          AND z_deposit_market_growth IS NOT NULL
          AND z_competitor_saturation IS NOT NULL
          AND z_unmet_mortgage_demand IS NOT NULL
         THEN round(w_hg * z_household_growth
                  + w_mi * z_median_income
                  + w_dg * z_deposit_market_growth
                  + w_cs * z_competitor_saturation
                  + w_um * z_unmet_mortgage_demand, 4)
    END                                         AS opportunity_score,

    -- Caveats travelling with the row rather than sitting in a footnote.
    (growth_basis <> 'direct')                  AS growth_is_estimated,
    (tract_status <> 'ok')                      AS tract_partly_unmeasured,
    (lmi_flag IS NULL)                          AS lmi_undetermined,
    (excluded_deposit_share > 0.25)             AS county_growth_heavily_adjusted,
    retail_basis_status
FROM weighted
ORDER BY opportunity_score DESC NULLS LAST;
