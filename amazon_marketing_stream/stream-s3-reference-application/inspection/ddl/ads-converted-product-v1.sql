-- Delivery inspection: querying raw deliveries for ads-converted-product-v1.
--
-- WHAT THIS IS FOR
--   Answering "what was actually delivered?". Which batch versions landed, what a sample
--   row looks like, how many delete markers a delivery carried, whether the schema matches
--   expectations.
--
-- WHAT THIS IS NOT FOR
--   Reporting. This table exposes every version of every record across every batch version.
--   There is no deduplication, no version resolution, and no delete handling. Any sum over it
--   double counts corrections and mixes superseded rows with current ones. It also gets
--   steadily more expensive as the restatement window fills, because every query scans all of
--   it. Use the current-state view for numbers.
--
-- WHY ONE TABLE PER DATASET
--   A single table at the bucket root with streamDatasetId as a partition cannot work. This
--   dataset has 189 columns; the other has a different count, and hourly and daily
--   datasets differ in partition depth because only hourly keys carry an hour= segment.
--
-- WHY PROJECTION RATHER THAN A CRAWLER
--   Partition projection computes partitions from the key layout. A crawler over tens of
--   thousands of objects is slow, bills per run, and leaves newly delivered partitions
--   invisible until it next runs.
--
-- CAVEAT ON COLUMN NAMES
--   Delivered names contain dots and Athena reads a dot as struct-field access. They are
--   quoted here. Verify against a real deployment before relying on this: if quoting is not
--   accepted, the alternative is a mapped table where each column is renamed and
--   'parquet.column.index.access' is enabled. The staging tables carry the same dotted names
--   and need the same quoting, differing only in case.

CREATE DATABASE IF NOT EXISTS delivery_raw;

CREATE EXTERNAL TABLE IF NOT EXISTS delivery_raw.raw_ads_converted_product_v1 (
  `streamSubscription.id` STRING,
  `streamDataset.id` STRING,
  `streamBatch.version` BIGINT,
  `date.value` STRING,
  `timeZone.value` STRING,
  `ad.id` STRING,
  `adGroup.id` STRING,
  `adProduct.value` STRING,
  `advertisedProduct.id` STRING,
  `advertisedProductMarketplace.value` STRING,
  `advertisedProductSku.value` STRING,
  `advertiserAccount.id` STRING,
  `advertiserAccountAlternateProfileId.value` STRING,
  `advertiserAccountAlternateDspAdvertiserId.value` STRING,
  `budgetCurrency.value` STRING,
  `campaign.id` STRING,
  `convertedProduct.id` STRING,
  `convertedProductMarketplace.value` STRING,
  `insertionOrder.id` STRING,
  `productRelevance.value` STRING,
  `metric.addToCart` BIGINT,
  `metric.addToCartFromClicks` BIGINT,
  `metric.addToCartFromClicksHalo` BIGINT,
  `metric.addToCartFromClicksPromoted` BIGINT,
  `metric.addToCartFromViews` BIGINT,
  `metric.addToCartFromViewsHalo` BIGINT,
  `metric.addToCartFromViewsPromoted` BIGINT,
  `metric.addToCartHalo` BIGINT,
  `metric.addToCartPromoted` BIGINT,
  `metric.addToList` BIGINT,
  `metric.addToListFromClicks` BIGINT,
  `metric.addToListFromClicksHalo` BIGINT,
  `metric.addToListFromClicksPromoted` BIGINT,
  `metric.addToListFromViews` BIGINT,
  `metric.addToListFromViewsHalo` BIGINT,
  `metric.addToListFromViewsPromoted` BIGINT,
  `metric.addToListHalo` BIGINT,
  `metric.addToListPromoted` BIGINT,
  `metric.addToWatchlist` BIGINT,
  `metric.addToWatchlistFromClicks` BIGINT,
  `metric.addToWatchlistFromViews` BIGINT,
  `metric.alexaSkillEnable` BIGINT,
  `metric.alexaSkillEnableFromClicks` BIGINT,
  `metric.alexaSkillEnableFromViews` BIGINT,
  `metric.alexaSkillInvocations` BIGINT,
  `metric.alexaSkillInvocationsFromClicks` BIGINT,
  `metric.alexaSkillInvocationsFromViews` BIGINT,
  `metric.appstoreFirstOpens` BIGINT,
  `metric.appstoreFirstOpensFromClicks` BIGINT,
  `metric.appstoreFirstOpensFromViews` BIGINT,
  `metric.appstoreFirstSessionHours` decimal(18,8),
  `metric.appstoreFirstSessionHoursFromClicks` decimal(18,8),
  `metric.appstoreFirstSessionHoursFromViews` decimal(18,8),
  `metric.appstoreOpens` BIGINT,
  `metric.appstoreOpensFromClicks` BIGINT,
  `metric.appstoreOpensFromViews` BIGINT,
  `metric.appstoreRepeatOpens` BIGINT,
  `metric.appstoreRepeatOpensFromClicks` BIGINT,
  `metric.appstoreRepeatOpensFromViews` BIGINT,
  `metric.appstoreRepeatSessionHours` decimal(18,8),
  `metric.appstoreRepeatSessionHoursFromClicks` decimal(18,8),
  `metric.appstoreRepeatSessionHoursFromViews` decimal(18,8),
  `metric.appstoreUsageHours` decimal(18,8),
  `metric.appstoreUsageHoursFromClicks` decimal(18,8),
  `metric.appstoreUsageHoursFromViews` decimal(18,8),
  `metric.appSubscriptionSignUps` BIGINT,
  `metric.appSubscriptionSignUpsFromClicks` BIGINT,
  `metric.appSubscriptionSignUpsFromViews` BIGINT,
  `metric.combinedPurchases` BIGINT,
  `metric.combinedPurchasesFromClicks` BIGINT,
  `metric.combinedPurchasesFromViews` BIGINT,
  `metric.combinedSales` decimal(18,8),
  `metric.combinedUnitsSold` BIGINT,
  `metric.detailPageViews` BIGINT,
  `metric.detailPageViewsFromClicks` BIGINT,
  `metric.detailPageViewsFromClicksHalo` BIGINT,
  `metric.detailPageViewsFromClicksPromoted` BIGINT,
  `metric.detailPageViewsFromViews` BIGINT,
  `metric.detailPageViewsFromViewsHalo` BIGINT,
  `metric.detailPageViewsFromViewsPromoted` BIGINT,
  `metric.detailPageViewsHalo` BIGINT,
  `metric.detailPageViewsPromoted` BIGINT,
  `metric.downloadedVideoPlays` BIGINT,
  `metric.downloadedVideoPlaysFromClicks` BIGINT,
  `metric.downloadedVideoPlaysFromViews` BIGINT,
  `metric.freeTrialAppSubscriptionSignUps` BIGINT,
  `metric.freeTrialAppSubscriptionSignUpsFromClicks` BIGINT,
  `metric.freeTrialAppSubscriptionSignUpsFromViews` BIGINT,
  `metric.freeTrialSubscriptionSignUps` BIGINT,
  `metric.freeTrialSubscriptionSignUpsFromClicks` BIGINT,
  `metric.freeTrialSubscriptionSignUpsFromViews` BIGINT,
  `metric.longTermSales` decimal(18,8),
  `metric.multiTouchPurchases` decimal(18,8),
  `metric.multiTouchPurchasesFromClicks` decimal(18,8),
  `metric.multiTouchPurchasesFromClicksHalo` decimal(18,8),
  `metric.multiTouchPurchasesFromClicksPromoted` decimal(18,8),
  `metric.multiTouchPurchasesFromViews` decimal(18,8),
  `metric.multiTouchPurchasesFromViewsHalo` decimal(18,8),
  `metric.multiTouchPurchasesFromViewsPromoted` decimal(18,8),
  `metric.multiTouchPurchasesHalo` decimal(18,8),
  `metric.multiTouchPurchasesPromoted` decimal(18,8),
  `metric.multiTouchSales` decimal(18,8),
  `metric.multiTouchSalesHalo` decimal(18,8),
  `metric.multiTouchSalesPromoted` decimal(18,8),
  `metric.multiTouchUnitsSold` decimal(18,8),
  `metric.multiTouchUnitsSoldHalo` decimal(18,8),
  `metric.multiTouchUnitsSoldPromoted` decimal(18,8),
  `metric.newToBrandDetailPageViews` BIGINT,
  `metric.newToBrandDetailPageViewsFromClicks` BIGINT,
  `metric.newToBrandDetailPageViewsFromClicksPromoted` BIGINT,
  `metric.newToBrandDetailPageViewsFromViews` BIGINT,
  `metric.newToBrandDetailPageViewsFromViewsPromoted` BIGINT,
  `metric.newToBrandDetailPageViewsPromoted` BIGINT,
  `metric.newToBrandPurchases` BIGINT,
  `metric.newToBrandPurchasesFromClicks` BIGINT,
  `metric.newToBrandPurchasesFromClicksHalo` BIGINT,
  `metric.newToBrandPurchasesFromClicksPromoted` BIGINT,
  `metric.newToBrandPurchasesFromViews` BIGINT,
  `metric.newToBrandPurchasesFromViewsHalo` BIGINT,
  `metric.newToBrandPurchasesFromViewsPromoted` BIGINT,
  `metric.newToBrandPurchasesHalo` BIGINT,
  `metric.newToBrandPurchasesPromoted` BIGINT,
  `metric.newToBrandSales` decimal(18,8),
  `metric.newToBrandSalesHalo` decimal(18,8),
  `metric.newToBrandSalesPromoted` decimal(18,8),
  `metric.newToBrandUnitsSold` BIGINT,
  `metric.newToBrandUnitsSoldHalo` BIGINT,
  `metric.newToBrandUnitsSoldPromoted` BIGINT,
  `metric.paidAppSubscriptionSignUps` BIGINT,
  `metric.paidAppSubscriptionSignUpsFromClicks` BIGINT,
  `metric.paidAppSubscriptionSignUpsFromViews` BIGINT,
  `metric.paidSubscriptionSignUps` BIGINT,
  `metric.paidSubscriptionSignUpsFromClicks` BIGINT,
  `metric.paidSubscriptionSignUpsFromViews` BIGINT,
  `metric.productReviewPageVisits` BIGINT,
  `metric.productReviewPageVisitsFromClicks` BIGINT,
  `metric.productReviewPageVisitsFromClicksHalo` BIGINT,
  `metric.productReviewPageVisitsFromClicksPromoted` BIGINT,
  `metric.productReviewPageVisitsFromViews` BIGINT,
  `metric.productReviewPageVisitsFromViewsHalo` BIGINT,
  `metric.productReviewPageVisitsFromViewsPromoted` BIGINT,
  `metric.productReviewPageVisitsPromoted` BIGINT,
  `metric.purchases` BIGINT,
  `metric.purchasesFromClicks` BIGINT,
  `metric.purchasesFromClicksHalo` BIGINT,
  `metric.purchasesFromClicksPromoted` BIGINT,
  `metric.purchasesFromViews` BIGINT,
  `metric.purchasesFromViewsHalo` BIGINT,
  `metric.purchasesFromViewsPromoted` BIGINT,
  `metric.purchasesHalo` BIGINT,
  `metric.purchasesPromoted` BIGINT,
  `metric.rentals` BIGINT,
  `metric.rentalsFromClicks` BIGINT,
  `metric.rentalsFromViews` BIGINT,
  `metric.sales` decimal(18,8),
  `metric.salesFromClicks` decimal(18,8),
  `metric.salesFromClicksPromoted` decimal(18,8),
  `metric.salesFromViews` decimal(18,8),
  `metric.salesFromViewsPromoted` decimal(18,8),
  `metric.salesHalo` decimal(18,8),
  `metric.salesPromoted` decimal(18,8),
  `metric.subscribeAndSaveSubscriptions` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromClicks` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromClicksHalo` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromClicksPromoted` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromViews` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromViewsHalo` BIGINT,
  `metric.subscribeAndSaveSubscriptionsFromViewsPromoted` BIGINT,
  `metric.subscribeAndSaveSubscriptionsHalo` BIGINT,
  `metric.subscribeAndSaveSubscriptionsPromoted` BIGINT,
  `metric.subscriptionSignUps` BIGINT,
  `metric.subscriptionSignUpsFromClicks` BIGINT,
  `metric.subscriptionSignUpsFromViews` BIGINT,
  `metric.trailerPlays` BIGINT,
  `metric.trailerPlaysFromClicks` BIGINT,
  `metric.trailerPlaysFromViews` BIGINT,
  `metric.unitsSold` BIGINT,
  `metric.unitsSoldFromClicks` BIGINT,
  `metric.unitsSoldFromClicksPromoted` BIGINT,
  `metric.unitsSoldFromViews` BIGINT,
  `metric.unitsSoldFromViewsPromoted` BIGINT,
  `metric.unitsSoldHalo` BIGINT,
  `metric.unitsSoldPromoted` BIGINT,
  `metric.videoStreamPlays` BIGINT,
  `metric.videoStreamPlaysFromClicks` BIGINT,
  `metric.videoStreamPlaysFromViews` BIGINT,
  `metric.brandedSearches` BIGINT,
  `metric.brandedSearchesFromClicks` BIGINT,
  `metric.brandedSearchesFromViews` BIGINT
)
PARTITIONED BY (
  `granularity` string,
  `year` string,
  `month` string,
  `day` string,
  `streambatchversion` string
)
STORED AS PARQUET
LOCATION 's3://YOUR_DELIVERY_BUCKET/streamDatasetId=ads-converted-product-v1/'
TBLPROPERTIES (
  'projection.enabled' = 'true'
  ,'projection.granularity.type' = 'enum'
  ,'projection.granularity.values' = 'daily'
  ,'projection.year.type' = 'integer'
  ,'projection.year.range' = '2025,2035'
  ,'projection.year.digits' = '4'
  ,'projection.month.type' = 'integer'
  ,'projection.month.range' = '1,12'
  ,'projection.month.digits' = '2'
  ,'projection.day.type' = 'integer'
  ,'projection.day.range' = '1,31'
  ,'projection.day.digits' = '2'
  ,'projection.streambatchversion.type' = 'injected'
  ,'storage.location.template' = 's3://YOUR_DELIVERY_BUCKET/streamDatasetId=ads-converted-product-v1/granularity=${granularity}/year=${year}/month=${month}/day=${day}/streamBatchVersion=${streambatchversion}'
);
