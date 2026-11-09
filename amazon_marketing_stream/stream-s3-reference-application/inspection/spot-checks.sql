-- ===========================================================================
-- Delivery inspection queries: what was actually delivered?
--
-- These run against the raw delivery bucket. They answer questions about the
-- DELIVERY, never about the data. Every version of every record is present here
-- with no deduplication, no version resolution, and no delete handling, so any
-- sum over this table double counts corrections and mixes superseded rows with
-- current ones.
--
-- For numbers, query current state with the tombstone filter:
--   FROM <catalog>.current.<dataset> WHERE is_deleted = false
-- There is no _live view on S3 Tables: neither Spark nor Athena can create a view in a
-- federated catalogue. See queries/current-state-view.sql.
--
-- Partition projection is enabled with streambatchversion 'injected', which
-- means every query MUST supply a streambatchversion predicate. That is not an
-- inconvenience: without one, a query would scan the entire restatement window.
--
-- Substitute the placeholders before running: <BATCH_VERSION_1> and
-- <BATCH_VERSION_2> are batch versions, <YYYY>, <MM> and <DD> are the arrival
-- date of a delivery, and <YYYY-MM-DD> is an event date. Left unsubstituted,
-- a query returns no rows rather than an error.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Which batch versions landed on a given day, and how much did each carry?
--
-- The first thing to run when someone reports missing or unexpected data.
-- Delivery is roughly every 15 minutes for the hourly dataset, so a day with
-- noticeably fewer versions than expected points at a delivery gap rather than
-- a pipeline problem.
-- ---------------------------------------------------------------------------
SELECT
    streambatchversion,
    count(*)                        AS row_count,
    count(DISTINCT "$path")         AS file_count,
    min("date.value")               AS earliest_event_date,
    max("date.value")               AS latest_event_date
FROM delivery_raw.raw_ads_performance_v1
WHERE granularity = 'hourly'
  AND year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion IN ('<BATCH_VERSION_1>', '<BATCH_VERSION_2>')
GROUP BY streambatchversion
ORDER BY streambatchversion;


-- ---------------------------------------------------------------------------
-- 2. Sample rows from one batch.
--
-- Confirms the shape of what arrived: are the dimensions populated as expected,
-- are the metrics where you expect them?
--
-- Note how many dimensions come back null. That is normal, not a fault: most
-- dimension columns are null on most rows, because a row carries only the
-- dimensions relevant to its own grain. This is also exactly why the staging
-- layer identifies records by a null-safe hash rather than by comparing key
-- columns directly.
-- ---------------------------------------------------------------------------
SELECT
    "date.value",
    "hour.value",
    "campaign.id",
    "adGroup.id",
    "searchTerm.value",
    "city.name",
    "metric.impressions",
    "metric.clicks",
    "metric.totalCost",
    "streamBatch.version"
FROM delivery_raw.raw_ads_performance_v1
WHERE granularity = 'hourly'
  AND year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion = '<BATCH_VERSION_1>'
LIMIT 50;


-- ---------------------------------------------------------------------------
-- 3. How many delete markers did a batch carry?
--
-- A delete marker is a row where EVERY metric column is null. This query tests
-- a handful of representative metrics, which is adequate for a spot check and
-- would NOT be adequate in the pipeline: a subset can only suggest, and the
-- definition requires all of them. The staging layer tests all 377.
--
-- Expect roughly 0% on large bulk or backfill deliveries, and close to 50% on
-- small restatement deliveries — a change to a mutable dimension is delivered as
-- a delete of the old value plus a create of the new one, so they arrive in
-- pairs. A ratio near 100% across a whole dataset is worth investigating.
-- ---------------------------------------------------------------------------
SELECT
    streambatchversion,
    count(*)                                                   AS total_rows,
    count_if(
        "metric.impressions" IS NULL
        AND "metric.clicks" IS NULL
        AND "metric.totalCost" IS NULL
        AND "metric.sales" IS NULL
        AND "metric.purchases" IS NULL
    )                                                          AS likely_delete_markers,
    round(
        100.0 * count_if(
            "metric.impressions" IS NULL
            AND "metric.clicks" IS NULL
            AND "metric.totalCost" IS NULL
            AND "metric.sales" IS NULL
            AND "metric.purchases" IS NULL
        ) / nullif(count(*), 0)
    , 1)                                                       AS delete_pct
FROM delivery_raw.raw_ads_performance_v1
WHERE granularity = 'hourly'
  AND year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion = '<BATCH_VERSION_1>'
GROUP BY streambatchversion;


-- ---------------------------------------------------------------------------
-- 4. Demonstration: why this table must not be used for reporting.
--
-- Run both halves. The raw figure is inflated by every superseded version of
-- every corrected record; the second is what the staging layer reports.
--
-- Both halves must name the same event date, <YYYY-MM-DD>, or they describe
-- different populations and the difference says nothing about version
-- resolution. The raw half keeps its arrival-date and batch-version predicates
-- as well, because partition projection requires them: it therefore shows the
-- duplication the two named deliveries carry for that event date, not every
-- version ever delivered for it, so treat the gap as a lower bound.
--
-- This query exists to be shown to anyone who proposes skipping the staging layer.
-- ---------------------------------------------------------------------------

-- Raw: counts every version, including superseded ones and delete markers.
SELECT
    'raw (wrong)'                    AS source,
    count(*)                         AS row_count,
    sum("metric.clicks")             AS clicks
FROM delivery_raw.raw_ads_performance_v1
WHERE granularity = 'hourly'
  AND year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion IN ('<BATCH_VERSION_1>', '<BATCH_VERSION_2>')
  AND "date.value" = '<YYYY-MM-DD>';

-- Resolved: one row per record, tombstones excluded.
-- Replace <catalog> with your catalog name.
SELECT
    'staging (correct)'              AS source,
    count(*)                         AS row_count,
    sum("metric.clicks")             AS clicks
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "date.value" = '<YYYY-MM-DD>';


-- ---------------------------------------------------------------------------
-- 5. Did the delivered schema change?
--
-- Cheap early warning. The pipeline validates types on every run and stops on a
-- lossy difference, but this shows the delivered types directly, which is useful
-- when reconciling against documentation.
--
-- Worth knowing: 119 of this dataset's 377 metrics are decimal(18,8), and they
-- carry monetary values and fractional attribution credit. Documentation has
-- described some of them as integers. Reading them as integers does not fail; it
-- truncates 0.37 to 0 and silently zeroes the affected metrics.
-- ---------------------------------------------------------------------------
SELECT
    column_name,
    data_type
FROM information_schema.columns
WHERE table_schema = 'delivery_raw'
  AND table_name = 'raw_ads_performance_v1'
  AND data_type LIKE 'decimal%'
ORDER BY column_name;


-- ===========================================================================
-- The daily dataset
--
-- Every query above targets the hourly raw table. The daily one has its own
-- table, generated by the same tool, and differs in exactly one way that
-- affects queries: there is no hour partition, because the delivered path has
-- no hour= segment. Substitute the table name and drop any hour predicate.
--
--   raw_ads_performance_v1        year, month, day, hour, streambatchversion
--   raw_ads_converted_product_v1  year, month, day,       streambatchversion
--
-- streambatchversion is still projected as 'injected', so it must still be
-- supplied in every query.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 6. What arrived in a delivery, daily dataset.
--
-- The daily equivalent of query 1. Note the absent hour predicate: adding one
-- returns nothing rather than erroring, because the column does not exist in
-- this table's projection.
-- ---------------------------------------------------------------------------
SELECT
    streambatchversion,
    count(*)                                                 AS rows_delivered,
    count(DISTINCT "convertedProduct.id")                    AS products,
    min("date.value")                                        AS earliest_event_date,
    max("date.value")                                        AS latest_event_date
FROM delivery_raw.raw_ads_converted_product_v1
WHERE year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion IN ('<BATCH_VERSION_1>', '<BATCH_VERSION_2>')
GROUP BY streambatchversion
ORDER BY streambatchversion;

-- ---------------------------------------------------------------------------
-- 7. Delete markers in a daily delivery.
--
-- A delete is every metric null with the dimensions still populated. This
-- samples a handful of metrics only; the pipeline checks all 169, because no
-- subset can establish deletion. Use this to eyeball, never to decide.
-- ---------------------------------------------------------------------------
SELECT
    count(*)                                                 AS rows_delivered,
    count_if(
        "metric.combinedUnitsSold" IS NULL
        AND "metric.combinedSales" IS NULL
        AND "metric.combinedPurchases" IS NULL
    )                                                        AS look_like_deletes
FROM delivery_raw.raw_ads_converted_product_v1
WHERE year = '<YYYY>' AND month = '<MM>' AND day = '<DD>'
  AND streambatchversion = '<BATCH_VERSION_1>';
