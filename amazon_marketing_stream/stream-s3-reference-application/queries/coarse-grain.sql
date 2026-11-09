-- ===========================================================================
-- Coarse-grain queries: the reporting tables.
--
-- These are pre-aggregated and rebuilt from current state on a schedule. Nine grains are
-- declared across the two datasets, listed at "Available grains per dataset" below;
-- campaign-day is the default when --grain is omitted, and the schedule passes --grain all.
-- They exist because ad-hoc aggregation over fine grain is slow and
-- expensive at scale — up to roughly 1.5 trillion rows in state at the largest
-- deployments — while a dashboard needs sub-second answers to the same handful of
-- questions.
--
-- Replace <catalog> with your catalog name throughout.
--
-- WHEN TO USE WHICH
--   coarse-grain   a question you knew you would ask. Fast and cheap.
--   current state  a question you did not. Any grain, slower.
--   inspection     a question about the delivery rather than the data.
--
-- The metric totals here are already summed across the sparse fine-grain rows, so
-- the "sum across all rows for an entity" caveat that applies to current state does not
-- apply when reading these. Derived measures still must divide aggregated
-- components rather than average per-row ratios.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Campaign performance over a period.
--
-- The shape most dashboards want. Reads one partition per day.
-- ---------------------------------------------------------------------------
SELECT
    "date.value",
    "campaign.id",
    "adproduct.value",
    "metric.impressions",
    "metric.clicks",
    "metric.totalcost",
    "metric.sales",
    "metric.purchases"
FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
ORDER BY "date.value", "metric.totalcost" DESC;


-- ---------------------------------------------------------------------------
-- 2. Top campaigns by spend, with derived measures.
--
-- Note the division happens after aggregation. Averaging per-row ratios would
-- weight a day with negligible spend equally with a day carrying most of it.
-- ---------------------------------------------------------------------------
SELECT
    "campaign.id",
    sum("metric.totalcost")                                              AS total_spend,
    sum("metric.clicks")                                                  AS clicks,
    sum("metric.impressions")                                             AS impressions,
    sum("metric.sales")                                                   AS sales,
    round(sum("metric.sales") / nullif(sum("metric.totalcost"), 0), 4)      AS roas,
    round(sum("metric.totalcost") / nullif(sum("metric.clicks"), 0), 4)     AS cost_per_click,
    round(100.0 * sum("metric.clicks") / nullif(sum("metric.impressions"), 0), 4) AS ctr_pct
FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "campaign.id"
ORDER BY total_spend DESC
LIMIT 20;


-- ---------------------------------------------------------------------------
-- 3. Day-over-day movement.
--
-- The kind of question that is cheap here and expensive against fine grain,
-- because the window function runs over a few thousand rows rather than billions.
-- ---------------------------------------------------------------------------
WITH daily AS (
    SELECT
        "date.value",
        sum("metric.totalcost") AS spend,
        sum("metric.sales")      AS sales
    FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
    WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
      AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    GROUP BY "date.value"
)
SELECT
    "date.value",
    spend,
    sales,
    lag(spend) OVER (ORDER BY "date.value")                                AS prior_spend,
    round(
        100.0 * (spend - lag(spend) OVER (ORDER BY "date.value"))
        / nullif(lag(spend) OVER (ORDER BY "date.value"), 0)
    , 2)                                                                 AS spend_change_pct
FROM daily
ORDER BY "date.value";


-- ---------------------------------------------------------------------------
-- 4. Reconciliation: does the coarse table agree with current state?
--
-- Run this after any change to the pipeline, and after a restatement rebuild. The
-- two figures must match exactly. A mismatch means a partition was not rebuilt
-- after its fine-grain rows were corrected, which is the one failure mode this
-- design has that produces stale rather than wrong numbers.
--
-- If they disagree, rebuild the affected dates:
--   stream-aggregate --dataset ads-performance-v1 --event-dates 2026-01-15 --grain <grain>
--
-- Name the grain the mismatch was found in. Without --grain the job rebuilds campaign_day
-- alone, which is its default, and leaves the grain you were looking at stale.
-- ---------------------------------------------------------------------------
WITH from_fine AS (
    SELECT
        "date.value",
        sum("metric.clicks")     AS clicks,
        sum("metric.totalcost") AS total_cost
    FROM "<catalog>".current.ads_performance_v1
    WHERE is_deleted = false
      AND "date.value" BETWEEN '2026-01-09' AND '2026-01-15'
    GROUP BY "date.value"
),
from_coarse AS (
    SELECT
        "date.value",
        sum("metric.clicks")     AS clicks,
        sum("metric.totalcost") AS total_cost
    FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
    WHERE "date.value" BETWEEN '2026-01-09' AND '2026-01-15'
    GROUP BY "date.value"
)
SELECT
    coalesce(f."date.value", c."date.value")                    AS "date.value",
    f.clicks                                                AS fine_clicks,
    c.clicks                                                AS coarse_clicks,
    f.clicks - c.clicks                                     AS click_difference,
    f.total_cost                                            AS fine_cost,
    c.total_cost                                            AS coarse_cost,
    f.total_cost - c.total_cost                             AS cost_difference,
    CASE
        WHEN f.clicks IS NULL THEN 'missing from fine grain'
        WHEN c.clicks IS NULL THEN 'partition never built'
        WHEN f.clicks = c.clicks AND f.total_cost = c.total_cost THEN 'match'
        ELSE 'MISMATCH - rebuild this date'
    END                                                     AS verdict
FROM from_fine f
FULL OUTER JOIN from_coarse c ON f."date.value" = c."date.value"
ORDER BY "date.value";


-- ---------------------------------------------------------------------------
-- 5. Scan-size comparison: why the coarse-grain table exists.
--
-- Run both and compare the bytes scanned reported by Athena, not the row counts.
-- The answers are identical; the cost is not. That difference is the entire
-- justification for maintaining a second table.
-- ---------------------------------------------------------------------------

-- Against fine grain.
SELECT sum("metric.clicks") AS clicks
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15';

-- Against coarse grain. Same answer.
SELECT sum("metric.clicks") AS clicks
FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15';


-- ===========================================================================
-- The daily converted-product dataset
--
-- Everything above targets the hourly performance dataset. These target the
-- daily one, and exist because it aggregates to a different grain: product,
-- not campaign. That grain groups by "convertedproduct.id", a column the
-- performance dataset does not have — which is why the grain is declared per
-- dataset in its schema file rather than once for the deployment.
--
-- Available grains per dataset:
--   ads-performance-v1        campaign_day, ad_group_day, campaign_hour,
--                             placement_day, device_day, geo_day
--   ads-converted-product-v1  campaign_day, product_day, halo_day
--
-- Ask for a grain a dataset does not declare and the job fails, naming the
-- ones it has, rather than building a table with that name and the wrong
-- grouping.
--
-- The scheduled run passes --grain all, so every grain listed above is rebuilt
-- on the aggregate cadence. Passing a single grain instead leaves every other
-- declared grain without a table until somebody runs the job by hand.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 6. Best-selling products over a period.
--
-- The question the product grain exists for. At campaign grain this is
-- unanswerable: the product column is not in the grouping, so the numbers
-- would be split across campaigns with no way to recombine them.
-- ---------------------------------------------------------------------------
SELECT
    "convertedproduct.id",
    sum("metric.combinedunitssold")                          AS units_sold,
    sum("metric.combinedsales")                               AS sales,
    sum("metric.combinedpurchases")                           AS purchases
FROM "<catalog>".aggregate.ads_converted_product_v1_product_day
WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "convertedproduct.id"
ORDER BY sales DESC
LIMIT 50;

-- ---------------------------------------------------------------------------
-- 7. Halo effect: sales of products that were not the ones advertised.
--
-- "advertisedproduct.id" and "convertedproduct.id" differ when an ad for one
-- product leads to the purchase of another. That comparison needs fine grain,
-- because the product grain keeps only the converted product.
-- ---------------------------------------------------------------------------
SELECT
    CASE
        WHEN "advertisedproduct.id" = "convertedproduct.id" THEN 'promoted'
        ELSE 'halo'
    END                                                      AS attribution,
    sum("metric.combinedsales")                               AS sales,
    sum("metric.combinedunitssold")                          AS units_sold
FROM "<catalog>".current.ads_converted_product_v1
WHERE is_deleted = false
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY 1
ORDER BY sales DESC;

-- ---------------------------------------------------------------------------
-- 8. Reconciliation across the two grains of one dataset.
--
-- Both are rebuilt from the same current-state table, with the tombstone predicate
-- applied inline, so a total that differs
-- between them means one of the two has a stale partition. Note this compares
-- the same dataset at two grains, not two datasets: the performance and
-- converted-product datasets count different events and are not expected to
-- agree with each other.
-- ---------------------------------------------------------------------------
WITH by_product AS (
    SELECT "date.value", sum("metric.combinedsales") AS sales
    FROM "<catalog>".aggregate.ads_converted_product_v1_product_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    GROUP BY "date.value"
),
by_campaign AS (
    SELECT "date.value", sum("metric.combinedsales") AS sales
    FROM "<catalog>".aggregate.ads_converted_product_v1_campaign_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    GROUP BY "date.value"
)
SELECT
    coalesce(p."date.value", c."date.value")                     AS "date.value",
    p.sales                                                  AS product_grain_sales,
    c.sales                                                  AS campaign_grain_sales,
    CASE
        WHEN p.sales IS NULL THEN 'product grain not built'
        WHEN c.sales IS NULL THEN 'campaign grain not built'
        WHEN p.sales = c.sales THEN 'match'
        ELSE 'MISMATCH - rebuild both for this date'
    END                                                      AS verdict
FROM by_product p
FULL OUTER JOIN by_campaign c ON p."date.value" = c."date.value"
ORDER BY "date.value";

-- ---------------------------------------------------------------------------
-- 9. Did every event date get an aggregate partition?
--
-- A gap here is the signature of a partition-overwrite fault: with Spark's
-- default STATIC overwrite mode, INSERT OVERWRITE replaces the whole table
-- rather than the dates it computes, so a run rebuilding two recent dates
-- removes every older partition. The jobs set DYNAMIC mode, and this query is
-- how you confirm it on a deployment.
-- ---------------------------------------------------------------------------
SELECT
    f."date.value",
    f.rows_in_current_state,
    a.rows_in_aggregate,
    CASE
        WHEN a.rows_in_aggregate IS NULL THEN 'AGGREGATE PARTITION MISSING'
        ELSE 'present'
    END                                                      AS verdict
FROM (
    SELECT "date.value", count(*) AS rows_in_current_state
    FROM "<catalog>".current.ads_converted_product_v1
    WHERE is_deleted = false
    GROUP BY "date.value"
) f
LEFT JOIN (
    SELECT "date.value", count(*) AS rows_in_aggregate
    FROM "<catalog>".aggregate.ads_converted_product_v1_product_day
    GROUP BY "date.value"
) a ON f."date.value" = a."date.value"
ORDER BY f."date.value";

-- ===========================================================================
-- Breakdown grains
--
-- Each of these groups by an attribute that campaign_day does not carry, so none
-- of them is a finer cut of campaign_day — they are different questions, and the
-- totals across any of them should agree with campaign_day for the same period.
-- That last property is what query 15 checks.
--
-- Note what they do NOT have: campaign.id. Dropping it is deliberate. Keeping it
-- would make each table strictly finer than campaign_day and no cheaper to read,
-- which defeats the reason for building a second table at all.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 10. Intraday shape: which hours the spend actually lands in.
--
-- Only campaign_hour can answer this. Every other grain sums the hours away, and
-- a day total cannot be split back into hours afterwards — the information is
-- gone once aggregated. That is the whole reason a separate grain exists for it
-- rather than a filter on campaign_day.
--
-- "hour.value" is an integer, so it sorts numerically here. Cast to a string
-- anywhere downstream and the axis reads 0, 1, 10, 11, 2.
-- ---------------------------------------------------------------------------
SELECT
    "hour.value",
    sum("metric.impressions")                                              AS impressions,
    sum("metric.clicks")                                                   AS clicks,
    sum("metric.totalcost")                                               AS spend,
    round(100.0 * sum("metric.clicks") / nullif(sum("metric.impressions"), 0), 4) AS ctr_pct
FROM "<catalog>".aggregate.ads_performance_v1_campaign_hour
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "hour.value"
ORDER BY "hour.value";


-- ---------------------------------------------------------------------------
-- 11. Spend and efficiency by device type.
--
-- The derived measures divide aggregated components rather than averaging per-row
-- ratios, for the reason stated at the top of this file: the components can sit on
-- different fine-grain rows, so a per-row ratio is not meaningful and averaging
-- those ratios weights a negligible row equally with a dominant one.
-- ---------------------------------------------------------------------------
SELECT
    "devicetype.value",
    "environment.value",
    sum("metric.impressions")                                              AS impressions,
    sum("metric.clicks")                                                   AS clicks,
    sum("metric.totalcost")                                               AS spend,
    round(sum("metric.totalcost") / nullif(sum("metric.clicks"), 0), 4)      AS cost_per_click
FROM "<catalog>".aggregate.ads_performance_v1_device_day
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "devicetype.value", "environment.value"
ORDER BY spend DESC;


-- ---------------------------------------------------------------------------
-- 12. Where the ads ran: placements ranked by spend.
--
-- "placementclassification.value" is carried alongside the name because placement
-- names are free text and vary by supply source, so the classification is what
-- groups comparable placements together.
-- ---------------------------------------------------------------------------
SELECT
    "placementclassification.value",
    "placement.name",
    sum("metric.impressions")                                              AS impressions,
    sum("metric.clicks")                                                   AS clicks,
    sum("metric.totalcost")                                               AS spend
FROM "<catalog>".aggregate.ads_performance_v1_placement_day
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "placementclassification.value", "placement.name"
ORDER BY spend DESC
LIMIT 25;


-- ---------------------------------------------------------------------------
-- 13. Geography.
--
-- "country.code" and "country.name" are both stored. The code is what a mapping tool
-- geocodes against; the name is what a table should display. Deriving one from the
-- other at query time would put a country lookup in every consumer.
--
-- Roll up to country by dropping "region.name" from the grouping. Regions are only
-- meaningful within a country, so grouping by region alone mixes identically-named
-- regions from different countries.
-- ---------------------------------------------------------------------------
SELECT
    "country.code",
    "country.name",
    "region.name",
    sum("metric.impressions")                                              AS impressions,
    sum("metric.clicks")                                                   AS clicks,
    sum("metric.totalcost")                                               AS spend
FROM "<catalog>".aggregate.ads_performance_v1_geo_day
WHERE "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY "country.code", "country.name", "region.name"
ORDER BY spend DESC
LIMIT 50;


-- ---------------------------------------------------------------------------
-- 14. Promoted versus halo, from a coarse table.
--
-- Compare with query 7, which answers the same question over fine grain because
-- the product grain keeps only the converted product. halo_day keeps both product
-- ids, so this reads a few thousand rows instead of the whole current-state table.
--
-- The CASE expression is duplicated in the QuickSight halo-day dataset on purpose:
-- one definition of the split, visible in version control in both places, rather
-- than a calculated field that only exists inside the dashboard.
-- ---------------------------------------------------------------------------
SELECT
    CASE
        WHEN "advertisedproduct.id" = "convertedproduct.id" THEN 'promoted'
        ELSE 'halo'
    END                                                      AS attribution,
    sum("metric.combinedsales")                               AS sales,
    sum("metric.combinedunitssold")                          AS units_sold
FROM "<catalog>".aggregate.ads_converted_product_v1_halo_day
WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY 1
ORDER BY sales DESC;


-- ---------------------------------------------------------------------------
-- 15. Cross-grain reconciliation for the breakdown tables.
--
-- Every grain of a dataset is rebuilt from the same current-state rows, so the
-- same metric summed over any of them must give the same figure for the same
-- period. A disagreement does not mean the aggregation is wrong: it means one of
-- these tables has a partition that was not rebuilt after its fine-grain rows
-- were corrected.
--
-- The one legitimate reason for a difference is a null grouping column. A row
-- whose "devicetype.value" is null still contributes to campaign_day, and still
-- contributes here — SQL groups nulls together rather than dropping them — so the
-- totals hold. If you add a grain that filters rows out, this query will report
-- the shortfall and it will not be a bug in the pipeline.
--
-- Fix a genuine mismatch by rebuilding that date across every grain:
--   stream-aggregate --dataset ads-performance-v1 --grain all --event-dates 2026-01-15
-- ---------------------------------------------------------------------------
WITH by_grain AS (
    SELECT 'campaign_day' AS grain, sum("metric.totalcost") AS spend
    FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    UNION ALL
    SELECT 'ad_group_day', sum("metric.totalcost")
    FROM "<catalog>".aggregate.ads_performance_v1_ad_group_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    UNION ALL
    SELECT 'campaign_hour', sum("metric.totalcost")
    FROM "<catalog>".aggregate.ads_performance_v1_campaign_hour
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    UNION ALL
    SELECT 'placement_day', sum("metric.totalcost")
    FROM "<catalog>".aggregate.ads_performance_v1_placement_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    UNION ALL
    SELECT 'device_day', sum("metric.totalcost")
    FROM "<catalog>".aggregate.ads_performance_v1_device_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
    UNION ALL
    SELECT 'geo_day', sum("metric.totalcost")
    FROM "<catalog>".aggregate.ads_performance_v1_geo_day
    WHERE "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
)
SELECT
    grain,
    spend,
    spend - min(spend) OVER ()                               AS difference_from_lowest,
    CASE
        WHEN spend IS NULL THEN 'GRAIN NEVER BUILT'
        WHEN spend = min(spend) OVER () AND spend = max(spend) OVER () THEN 'match'
        ELSE 'MISMATCH - rebuild this dataset with --grain all'
    END                                                      AS verdict
FROM by_grain
ORDER BY grain;

-- ---------------------------------------------------------------------------
-- 16. Does any coarse date hold totals for data that no longer exists?
--
-- The companion to query 9. That one finds an event date with no aggregate
-- partition; this one finds an aggregate partition whose fine-grain rows have
-- all been deleted, which is the opposite failure and does not look like one:
-- the totals are present, internally consistent, and simply describe data that
-- has been retracted.
--
-- A dynamic partition overwrite replaces only the partitions its query emits.
-- A date whose every row is a tombstone emits none, so an overwrite on its own
-- leaves the old totals in place and clears the date's rebuild mark as though
-- the work had been done. The rebuild deletes the event dates before refilling
-- them, so this query should return no rows.
--
-- A row here means those totals will keep being reported until someone rebuilds
-- that date by hand. Any row is a defect, not a threshold to tune.
--
-- Restricted to dates the fine-grain table knows about. A coarse date with no
-- fine-grain rows at all is a different situation -- retention has removed the
-- detail and the aggregate is the only remaining record -- and that is expected
-- rather than wrong.
-- ---------------------------------------------------------------------------
WITH live_by_date AS (
    SELECT
        "date.value"                                          AS event_date,
        count(*)                                              AS live_rows
    FROM "<catalog>".current.ads_performance_v1
    WHERE is_deleted = false
    GROUP BY "date.value"
),
all_by_date AS (
    SELECT
        "date.value"                                          AS event_date,
        count(*)                                              AS total_rows
    FROM "<catalog>".current.ads_performance_v1
    GROUP BY "date.value"
),
coarse_by_date AS (
    SELECT
        "date.value"                                          AS event_date,
        count(*)                                              AS coarse_rows,
        sum("metric.totalcost")                               AS coarse_spend
    FROM "<catalog>".aggregate.ads_performance_v1_campaign_day
    GROUP BY "date.value"
)
SELECT
    c.event_date,
    c.coarse_rows,
    c.coarse_spend,
    coalesce(l.live_rows, 0)                                  AS live_fine_grain_rows,
    a.total_rows                                              AS fine_grain_rows_including_deleted,
    'STALE - every fine-grain row for this date is deleted, rebuild it'  AS verdict
FROM coarse_by_date c
-- Inner join: the date must still exist in the fine-grain table, so this cannot
-- fire for a date whose detail has aged out under retention.
JOIN all_by_date a ON c.event_date = a.event_date
LEFT JOIN live_by_date l ON c.event_date = l.event_date
WHERE coalesce(l.live_rows, 0) = 0
  AND c.coarse_rows > 0
ORDER BY c.event_date;
