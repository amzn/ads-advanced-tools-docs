-- ===========================================================================
-- Report types: the seven standard breakdowns, built from delivered Stream data.
--
-- Each query below reproduces one familiar advertising report at its own level of
-- detail. They read the current-state tables, not the coarse-grain tables, because
-- these levels of detail are not pre-aggregated — a deployment declares a handful of
-- grains, and target, search term and creative are not among them. That makes these
-- queries slower and more expensive than the coarse-grain ones in `coarse-grain.sql`,
-- and correct at any grain you ask for.
--
-- Every query carries `WHERE is_deleted = false`, and it is not optional. A deleted
-- record is kept as a tombstone rather than removed, so omitting the predicate counts
-- deletions as live rows — a wrong total rather than an error. It is filtered inline
-- rather than by reading a `_live` view because on the S3 Tables path no such view can
-- exist: neither engine reaching S3 Tables implements Iceberg's view interface. See
-- `current-state-view.sql`.
--
-- Replace <catalog> with your catalog name and REPLACE_WITH_ACCOUNT_ID with your
-- advertiser account id throughout.
--
-- ---------------------------------------------------------------------------
-- WHY THERE IS NO COUNTRY FILTER, THOUGH THE FAMILIAR REPORTS HAVE ONE
--
-- One dataset carries every ad product, and a row carries only the fields its own ad
-- product delivers. So a dimension being null is normal rather than missing, and a
-- filter on the wrong one silently excludes whole ad products.
--
-- On Sponsored Products rows, for example:
--
--     searchTerm.value       populated
--     country.code           null
--     deviceType.value       null
--     matchedTarget.value    null
--
-- `country.code` is the geo of the impression, and Sponsored Products does not deliver
-- it. `country.code = 'US'` therefore removes every Sponsored Products row — not
-- because there is no US traffic, but because the column those rows would be matched
-- on is empty. That is the failure mode worth naming: an unpopulated dimension in a
-- WHERE clause is indistinguishable from a filter that legitimately matched nothing.
--
-- The country predicate is therefore left commented out in each report below. Enable it
-- only for ad products that deliver impression geo, such as display and DSP. If you
-- want a marketplace rather than an impression country, the delivered field is
-- `advertisedProductMarketplace.value` — the marketplace the advertised product is listed
-- in, which the daily dataset supplements with `convertedProductMarketplace.value` for the
-- converted product. Neither one says where the impression was served.
--
-- `matchedTarget.value` is null on those rows too. Reports 1 and 3 still work, since
-- `adGroupTarget.id` identifies the target, but the readable expression is empty.
--
-- Check any dimension against your own data before filtering on it:
--
--     SELECT "adProduct.value", count(*), count("country.code"), count("matchedTarget.value")
--     FROM "<catalog>".current.ads_performance_v1
--     WHERE is_deleted = false
--     GROUP BY "adProduct.value";
--
-- The query above is what settles it for a given deployment.
-- ---------------------------------------------------------------------------
--
-- ---------------------------------------------------------------------------
-- THREE REQUESTED FIELDS DO NOT EXIST, AND ARE NOT SUBSTITUTED SILENTLY
--
--   target.matchType        No equivalent is delivered. Exact, phrase and broad
--                           cannot be distinguished here. Reports 1 and 3 are
--                           therefore at target grain, not target-and-match-type
--                           grain, and two match types on the same target collapse
--                           into one row.
--
--   adGroupTarget.bid       A bid is campaign *configuration*, not an event. Nothing
--                           in a delivered record carries it. It has to come from the
--                           campaign management API and be joined in.
--
--   campaign.country        Not delivered. `country.code` and `country.name` are
--                           delivered, but they are the country the *impression*
--                           occurred in, not the marketplace the campaign belongs to.
--                           For a single-marketplace advertiser the two coincide and
--                           the filter below is equivalent. For an advertiser running
--                           in several marketplaces they do not, and filtering on
--                           `country.code` answers "impressions served in the US"
--                           rather than "campaigns in the US marketplace".
--
-- Two more were named differently rather than missing: `advertisedProduct.sku` is
-- delivered as `advertisedProductSku.value`, and `target.value` as
-- `matchedTarget.value` — the target that matched, which is the useful reading.
-- ---------------------------------------------------------------------------
--
-- WHY EVERY METRIC IS SUMMED
--
-- A single entity spans several fine-grain rows, with different metrics populated on
-- each, because metrics that do not apply to a row arrive as zero and sit elsewhere.
-- Summing across all of them is what produces the right total. Never read a metric
-- off one row.
--
-- Derived measures must divide *aggregated* components — sum the clicks, sum the
-- impressions, then divide. Computing a rate per row and averaging the rates gives the
-- wrong answer, because the numerator and denominator can live on different rows.
--
-- CONVERSIONS ARE SPLIT PROMOTED / HALO, NOT BY LOOKBACK WINDOW
--
-- There are no 1d/7d/14d/30d columns. Stream does not deliver a lookback dimension,
-- so a window cannot be recovered from a total — it is decided upstream, at
-- attribution time. What is delivered instead is a split by *what* converted:
-- `Promoted` is the advertised product itself, `Halo` is everything else the
-- advertiser sells. `metric.purchases` is the total; the two parts sum to it.
--
-- Numbers from these queries will therefore not tie to a pull-API report. The two
-- systems attribute differently, and neither is a rounding error of the other.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Targeting — target-level performance.
--
-- Requested match type is not delivered, so this is target grain. `adgrouptarget.id`
-- is the target's identity and `matchedtarget.value` is its expression.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                       AS event_date,
    "campaign.id"                      AS campaign_id,
    "adgroup.id"                       AS ad_group_id,
    "adgrouptarget.id"                 AS target_id,
    "matchedtarget.value"              AS target_expression,
    "adproduct.value"                  AS ad_product,
    "budgetcurrency.value"             AS currency,
    "country.code"                     AS impression_country,
    sum("metric.impressions")          AS impressions,
    sum("metric.clicks")               AS clicks,
    sum("metric.totalcost")            AS cost,
    sum("metric.purchases")            AS purchases,
    sum("metric.purchasespromoted")    AS purchases_promoted,
    sum("metric.purchaseshalo")        AS purchases_halo,
    sum("metric.sales")                AS sales,
    sum("metric.unitssold")            AS units_sold
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY impressions DESC;


-- ---------------------------------------------------------------------------
-- 2. Placement — where the ad ran.
--
-- There is a `placement_day` coarse table, but it is not a cheaper drop-in for this
-- query. It is grouped by date, account, ad product, `placement.name` and
-- `placementclassification.value` only — so it carries no campaign, no currency and no
-- country. Reading it instead answers a narrower question, rather than this one faster.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                       AS event_date,
    "campaign.id"                      AS campaign_id,
    "placementclassification.value"    AS placement_classification,
    "adproduct.value"                  AS ad_product,
    "budgetcurrency.value"             AS currency,
    "country.code"                     AS impression_country,
    sum("metric.impressions")          AS impressions,
    sum("metric.clicks")               AS clicks,
    sum("metric.totalcost")            AS cost,
    sum("metric.purchases")            AS purchases,
    sum("metric.sales")                AS sales,
    sum("metric.unitssold")            AS units_sold
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY impressions DESC;


-- ---------------------------------------------------------------------------
-- 3. Search Term — what shoppers actually typed, against the target it matched.
--
-- The widest of the seven. Search term is high-cardinality, so narrow the date range
-- before widening anything else.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                       AS event_date,
    "campaign.id"                      AS campaign_id,
    "adgroup.id"                       AS ad_group_id,
    "adgrouptarget.id"                 AS target_id,
    "matchedtarget.value"              AS target_expression,
    "searchterm.value"                 AS search_term,
    "adproduct.value"                  AS ad_product,
    "budgetcurrency.value"             AS currency,
    sum("metric.impressions")          AS impressions,
    sum("metric.clicks")               AS clicks,
    sum("metric.totalcost")            AS cost,
    sum("metric.purchases")            AS purchases,
    sum("metric.sales")                AS sales,
    sum("metric.unitssold")            AS units_sold
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2026-01-08' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
  AND "searchterm.value" IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY clicks DESC
LIMIT 1000;


-- ---------------------------------------------------------------------------
-- 4. Advertised Product — ASIN and SKU level performance.
--
-- `advertisedproduct.id` is the ASIN, `advertisedproductsku.value` the seller's own
-- SKU. Both are carried because one ASIN can be sold under several SKUs.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                            AS event_date,
    "campaign.id"                           AS campaign_id,
    "adgroup.id"                            AS ad_group_id,
    "ad.id"                                 AS ad_id,
    "advertisedproduct.id"                  AS advertised_asin,
    "advertisedproductsku.value"            AS advertised_sku,
    "advertisedproductmarketplace.value"    AS marketplace,
    "adproduct.value"                       AS ad_product,
    "budgetcurrency.value"                  AS currency,
    sum("metric.impressions")               AS impressions,
    sum("metric.clicks")                    AS clicks,
    sum("metric.totalcost")                 AS cost,
    sum("metric.purchases")                 AS purchases,
    sum("metric.purchasespromoted")         AS purchases_promoted,
    sum("metric.purchaseshalo")             AS purchases_halo,
    sum("metric.sales")                     AS sales,
    sum("metric.salespromoted")             AS sales_promoted,
    sum("metric.saleshalo")                 AS sales_halo,
    sum("metric.unitssold")                 AS units_sold
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
ORDER BY sales DESC;


-- ---------------------------------------------------------------------------
-- 5. Converted Product — which products were bought, not which were advertised.
--
-- The only query here reading the daily dataset, and the only one that answers what a
-- click on one product caused someone to buy. `convertedproduct.id` is the purchased
-- ASIN; when it differs from the advertised one, that is halo.
--
-- NO SPEND. The daily dataset delivers no impressions, clicks or cost, so cost per
-- converted product cannot be computed here. Join report 4 on campaign and date for
-- spend, accepting that the cost belongs to the advertised product rather than the
-- converted one.
--
-- `adproduct.value` is where a Sponsored Brands filter goes if you want one; it is
-- left unfiltered so the query works for every ad product.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                            AS event_date,
    "campaign.id"                           AS campaign_id,
    "adgroup.id"                            AS ad_group_id,
    "convertedproduct.id"                   AS converted_asin,
    "convertedproductmarketplace.value"     AS converted_marketplace,
    "productrelevance.value"                AS product_relevance,
    "adproduct.value"                       AS ad_product,
    "budgetcurrency.value"                  AS currency,
    sum("metric.purchases")                 AS purchases,
    sum("metric.purchasespromoted")         AS purchases_promoted,
    sum("metric.purchaseshalo")             AS purchases_halo,
    sum("metric.sales")                     AS sales,
    sum("metric.salespromoted")             AS sales_promoted,
    sum("metric.saleshalo")                 AS sales_halo,
    sum("metric.unitssold")                 AS units_sold,
    sum("metric.unitssoldpromoted")         AS units_sold_promoted,
    sum("metric.unitssoldhalo")             AS units_sold_halo
FROM "<catalog>".current.ads_converted_product_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY sales DESC;


-- ---------------------------------------------------------------------------
-- 6. Ad Creative — performance per ad.
--
-- The same shape as report 4 with the product dimensions dropped, which makes it the
-- one to use when comparing creatives rather than catalogue entries.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                       AS event_date,
    "campaign.id"                      AS campaign_id,
    "adgroup.id"                       AS ad_group_id,
    "ad.id"                            AS ad_id,
    "adproduct.value"                  AS ad_product,
    "budgetcurrency.value"             AS currency,
    "country.code"                     AS impression_country,
    sum("metric.impressions")          AS impressions,
    sum("metric.clicks")               AS clicks,
    sum("metric.totalcost")            AS cost,
    sum("metric.purchases")            AS purchases,
    sum("metric.sales")                AS sales,
    sum("metric.unitssold")            AS units_sold
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY impressions DESC;


-- ---------------------------------------------------------------------------
-- 7. Hourly Campaign — intraday performance.
--
-- The one report that only this pipeline can answer cheaply: `hour.value` is delivered
-- on every row of the hourly dataset, so intraday is a group-by rather than a separate
-- product.
--
-- The `campaign_hour` coarse table is grouped by date, hour, account, ad product and
-- campaign. That is this report minus currency and country, so it is a genuine
-- substitute for the reduced form — and only for the reduced form.
--
-- The two-week limit in the familiar report is a retention limit of the system it comes
-- from, not one here. How far back this can go is set by the restatement window — 70 days —
-- and by how long the delivery bucket keeps its objects.
-- ---------------------------------------------------------------------------
SELECT
    "date.value"                       AS event_date,
    "hour.value"                       AS event_hour,
    "campaign.id"                      AS campaign_id,
    "adproduct.value"                  AS ad_product,
    "budgetcurrency.value"             AS currency,
    "country.code"                     AS impression_country,
    sum("metric.impressions")          AS impressions,
    sum("metric.clicks")               AS clicks,
    sum("metric.totalcost")            AS cost,
    sum("metric.purchases")            AS purchases,
    sum("metric.sales")                AS sales
FROM "<catalog>".current.ads_performance_v1
WHERE is_deleted = false
  AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
  AND "date.value" BETWEEN '2026-01-01' AND '2026-01-15'
  -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY event_date, event_hour;


-- ---------------------------------------------------------------------------
-- 8. Derived measures, computed the only way that is correct.
--
-- Not one of the seven. Included because every one of them gets asked for a CTR, a
-- CPC or a ROAS next, and doing it per row then averaging is the most common way to
-- get a plausible wrong number out of this data.
--
-- Note the guards: a campaign with no impressions has no CTR, and dividing by zero in
-- Athena yields a division error rather than a null.
-- ---------------------------------------------------------------------------
WITH totals AS (
    SELECT
        "date.value"                   AS event_date,
        "campaign.id"                  AS campaign_id,
        sum("metric.impressions")      AS impressions,
        sum("metric.clicks")           AS clicks,
        sum("metric.totalcost")        AS cost,
        sum("metric.sales")            AS sales,
        sum("metric.purchases")        AS purchases
    FROM "<catalog>".current.ads_performance_v1
    WHERE is_deleted = false
      AND "advertiseraccount.id" = 'REPLACE_WITH_ACCOUNT_ID'
      AND "date.value" BETWEEN '2025-12-16' AND '2026-01-15'
      -- AND "country.code" = 'US'    -- excludes every Sponsored Products row; see the header
    GROUP BY 1, 2
)
SELECT
    event_date,
    campaign_id,
    impressions,
    clicks,
    cost,
    sales,
    purchases,
    if(impressions > 0, clicks * 1.0 / impressions)        AS click_through_rate,
    if(clicks > 0, cost / clicks)                          AS cost_per_click,
    if(clicks > 0, purchases * 1.0 / clicks)               AS conversion_rate,
    if(cost > 0, sales / cost)                             AS return_on_ad_spend
FROM totals
ORDER BY cost DESC;
