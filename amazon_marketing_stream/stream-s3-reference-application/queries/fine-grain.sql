-- ===========================================================================
-- Staging-layer queries: the fine-grain current-state table.
--
-- Every query below carries WHERE is_deleted = false, and it is not optional. Deleted records
-- are retained in the table as tombstones so that a later out-of-order delivery carrying an
-- older version cannot resurrect them, so excluding them is the reader's job. Omit the
-- predicate and you get deleted records in your totals, with nothing to tell you.
--
-- There is no _live view to query instead. On S3 Tables a view cannot be created from either
-- engine: Spark fails because no catalogue reaching S3 Tables implements Iceberg's view
-- interface, and Athena fails with "CREATE VIEW statements are not yet supported for
-- Cross-Account Glue DataCatalogs" because the integration exposes a table bucket as a
-- federated catalogue. See queries/current-state-view.sql.
--
-- The two exceptions below are marked where they occur: the health query and the
-- record-inspection query deliberately do not filter, because both are there to show
-- tombstones.
--
-- Replace <catalog> with your catalog name throughout.
--
-- TWO PROPERTIES OF THIS DATASET THAT AFFECT EVERY QUERY
--
-- 1. Rows are sparse and an entity spans several of them. Metrics that do not
--    apply to a row arrive as zero and sit on different rows from the metrics
--    that do. A single campaign-hour therefore appears as multiple rows with
--    different metrics populated. Summing across all of them gives the correct
--    total; filtering to "the row with the impressions" does not.
--
-- 2. Derived measures must aggregate their components separately. Return on ad
--    spend is sum(sales) / sum(cost), never avg(sales/cost) — the numerator and
--    denominator can live on different rows, so a per-row ratio is meaningless.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Inspect one campaign at full grain.
--
-- The date predicate is not optional. "date.value" is the leading partition
-- column, so without it this scans the entire restatement window.
-- ---------------------------------------------------------------------------
SELECT
    "date.value",
    "hour.value",
    "campaign.id",
    "adgroup.id",
    "searchterm.value",
    "placementclassification.value",
    "city.name",
    "devicetype.value",
    "metric.impressions",
    "metric.clicks",
    "metric.totalcost",
    "streambatch.version"
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "date.value" = '2026-01-15'
  AND "campaign.id" = 'REPLACE_WITH_CAMPAIGN_ID'
ORDER BY "hour.value", "metric.impressions" DESC
LIMIT 200;


-- ---------------------------------------------------------------------------
-- 2. Aggregate on the fly, at whatever grain you need.
--
-- This is the point of keeping fine grain: any grain is available without a
-- pipeline. It is also slower and more expensive than querying the coarse-grain
-- table, so it suits exploration rather than a dashboard.
-- ---------------------------------------------------------------------------
SELECT
    "date.value",
    "campaign.id",
    sum("metric.impressions")                                     AS impressions,
    sum("metric.clicks")                                          AS clicks,
    sum("metric.totalcost")                                      AS total_cost,
    sum("metric.sales")                                           AS sales,
    -- Components aggregated separately, then divided. See the header note.
    round(sum("metric.sales") / nullif(sum("metric.totalcost"), 0), 4)  AS roas,
    round(100.0 * sum("metric.clicks") / nullif(sum("metric.impressions"), 0), 4) AS ctr_pct
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
GROUP BY "date.value", "campaign.id"
ORDER BY "date.value", total_cost DESC;


-- ---------------------------------------------------------------------------
-- 3. A grain the coarse table does not carry: search term by city.
--
-- Worth running once to see the value of the staging layer. This question cannot
-- be answered from a campaign-day table at all, and needs no new pipeline here.
-- ---------------------------------------------------------------------------
SELECT
    "searchterm.value",
    "city.name",
    sum("metric.clicks")      AS clicks,
    sum("metric.totalcost")  AS total_cost,
    sum("metric.sales")       AS sales
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "date.value" BETWEEN '2026-01-09' AND '2026-01-15'
  AND "campaign.id" = 'REPLACE_WITH_CAMPAIGN_ID'
  AND "searchterm.value" IS NOT NULL
GROUP BY "searchterm.value", "city.name"
HAVING sum("metric.clicks") > 0
ORDER BY clicks DESC
LIMIT 100;


-- ---------------------------------------------------------------------------
-- 4. Product-level conversions, from the daily dataset.
--
-- Note the absence of "hour.value": this dataset is delivered daily and has no
-- hour dimension. "productrelevance.value" distinguishes products the campaign
-- promoted directly from related products that converted alongside them.
-- ---------------------------------------------------------------------------
SELECT
    "date.value",
    "campaign.id",
    "convertedproduct.id",
    "productrelevance.value",
    sum("metric.purchases")   AS purchases,
    sum("metric.sales")       AS sales,
    sum("metric.unitssold")  AS units_sold
FROM "<catalog>".current.ads_converted_product_v1
WHERE is_deleted = false
  AND "date.value" BETWEEN '2026-01-09' AND '2026-01-15'
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
GROUP BY "date.value", "campaign.id", "convertedproduct.id", "productrelevance.value"
ORDER BY sales DESC
LIMIT 100;


-- ===========================================================================
-- OPERATIONAL QUERIES
-- These inspect the pipeline rather than the advertising data.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 5. Is the pipeline current?
--
-- Compare max_batch_version against the newest version seen on the queue. Batch
-- version is the only clock both sides share, so lag is expressed in version
-- units rather than converted to time, which would require assuming its
-- encoding.
--
-- tombstone_ratio is worth watching. Bulk deliveries carry no deletes and
-- restatement deliveries roughly half, so a ratio climbing toward 1.0 across the
-- whole table is what a mass negating delivery would look like.
-- ---------------------------------------------------------------------------
SELECT
    count(*)                                                     AS total_rows,
    count_if(is_deleted)                                         AS tombstones,
    round(1.0 * count_if(is_deleted) / nullif(count(*), 0), 4)    AS tombstone_ratio,
    max("streambatch.version")                                     AS max_batch_version,
    min("date.value")                                               AS earliest_event_date,
    max("date.value")                                               AS latest_event_date
-- No is_deleted filter here, deliberately: this query measures tombstones, so excluding
-- them would report a ratio of zero however many there are.
FROM "<catalog>".current.ads_performance_v1;


-- ---------------------------------------------------------------------------
-- 6. Provenance: which delivered object produced a given row?
--
-- source_uri is carried through ingest for exactly this. When a value is
-- questioned, this answers it without guesswork.
-- ---------------------------------------------------------------------------
SELECT
    record_key,
    "streambatch.version",
    is_deleted,
    source_uri
-- No is_deleted filter: the point is to see a record's delete state, including tombstones.
FROM "<catalog>".current.ads_performance_v1
WHERE "date.value" = '2026-01-15'
  AND "campaign.id" = 'REPLACE_WITH_CAMPAIGN_ID'
LIMIT 20;


-- ---------------------------------------------------------------------------
-- 7. Ingest manifest: what has been consumed, and what merge will read?
--
-- Three things at once. It is the deduplication ledger: if an object appears
-- here it will not be reprocessed, which is how at-least-once delivery is
-- absorbed. It is the merge work list: merge asks it which objects arrived on a
-- date and reads those objects, so "replay the 14th" is a question only this
-- table can answer. And rows_consumed is the reconciliation baseline -- the row
-- count is taken from each object's Parquet footers at ingest, so if a merge
-- later reads fewer rows than this, delivered objects have gone missing.
--
-- Partitioned on ingest_date, which is ARRIVAL date, not event date. A
-- correction for last Tuesday arrives today; it appears under today.
--
-- Note this table is never pruned. Deleting a row would readmit its object for
-- reprocessing. It stays small because it holds one row per object, not per row.
-- ---------------------------------------------------------------------------
SELECT
    ingest_date,
    count(*)                    AS objects_consumed,
    sum(row_count)              AS rows_consumed,
    count(DISTINCT event_date)  AS event_dates_touched,
    min(batch_version)          AS lowest_batch_version,
    max(batch_version)          AS highest_batch_version,
    min(ingested_at)            AS first_consumed,
    max(ingested_at)            AS last_consumed
FROM "<catalog>".ingest.ads_performance_v1_manifest
WHERE ingest_date >= '2026-09-01'
GROUP BY ingest_date
ORDER BY ingest_date DESC;


-- ---------------------------------------------------------------------------
-- 8. Which objects produced a given event date?
--
-- A record's full restatement history is not one query here, because there is no
-- landing table holding every version of every row: use this to find the objects
-- that carried the event date, then read those objects directly with the external
-- tables in inspection/ddl/ and filter on the record's key columns. Slower, and
-- bounded by the delivery bucket's retention rather than by a window this
-- application controls.
--
-- The inverse of the partitioning: event_date is a column here, not the
-- partition, so this shows every arrival that contributed to one day of events.
-- A day with many distinct ingest_dates is a day that has been restated
-- repeatedly, which is the normal shape for recent dates.
--
-- This is also the list to pass to `stream-merge --ingest-dates` to rebuild that
-- event date from the delivered objects.
-- ---------------------------------------------------------------------------
SELECT
    event_date,
    ingest_date,
    count(*)        AS objects,
    sum(row_count)  AS rows_delivered,
    max(batch_version) AS highest_batch_version
FROM "<catalog>".ingest.ads_performance_v1_manifest
WHERE event_date = '2026-01-15'
GROUP BY event_date, ingest_date
ORDER BY ingest_date;
