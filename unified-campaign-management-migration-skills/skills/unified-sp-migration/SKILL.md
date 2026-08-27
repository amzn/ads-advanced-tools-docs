---
name: unified-sp-migration
description: "Use when migrating Sponsored Products campaigns from SP v3 API (/sp/) to the Unified API (/adsApi/v1/), including endpoint mapping, request body restructuring, header changes, field mapping differences, bidding/targeting migration, and troubleshooting migration errors."
---

# Sponsored Products: SP v3 API to Unified API Migration Guide

## 1. Migration Overview

This guide helps API developers migrate from the **Sponsored Products v3 API** (paths like `/sp/...`) to the **Unified API** (paths like `/adsApi/v1/...`).

### Key Architectural Changes

| Aspect | SP v3 API | Unified API |
|--------|-----------|-------------|
| Scope | SP-only API | Shared across all ad products (SP, SB, etc.) |
| Endpoint pattern | `/sp/{resource}` | `/adsApi/v1/{action}/{resource}` |
| HTTP methods | Mixed (POST, PUT, GET, DELETE) | All `POST` |
| Content-Type | Versioned per resource (e.g. `application/vnd.spAdGroup.v3+json`) | Standard `application/json` |
| Targeting model | Separate endpoints for keywords, targets, negative keywords, negative targets | Unified `/targets` endpoint with `targetType` and `negative` flag |
| Product Ads | Dedicated `/sp/productAds` endpoint | Unified `/ads` endpoint |
| Ad product identifier | Implicit (from path) | Explicit `adProduct: "SPONSORED_PRODUCTS"` required |

### Confidence Legend

- ✅ **Confirmed from schema** — directly verifiable from API spec field names, types, and enum values
- ⚠️ **Reasonable inference** — based on semantic analysis and naming patterns; not from an official migration guide
- ❓ **Unconfirmed** — cannot be determined from schema alone; open a support case with Amazon Ads API team

---

## 2. Header Changes

| Header | SP v3 API | Unified API | Confidence |
|--------|-----------|-------------|------------|
| Client ID | `Amazon-Advertising-API-ClientId` | `Amazon-Ads-ClientId` | ✅ Confirmed |
| Profile/Scope | `Amazon-Advertising-API-Scope` (required) | `Amazon-Advertising-API-Scope` (optional) | ✅ Confirmed |
| Account ID | Not used | `Amazon-Ads-AccountId` (optional) | ✅ Confirmed |
| Content-Type | Versioned: `application/vnd.spAdGroup.v3+json`, `application/vnd.spCampaign.v3+json`, etc. | Standard: `application/json` | ✅ Confirmed |
| Prefer | `Prefer` header (optional) | Not used | ✅ Confirmed |

---

## 3. Endpoint Path Mapping

### Core CRUD Operations

| Operation | SP v3 API | Unified API |
|-----------|-----------|-------------|
| Create Campaign | `POST /sp/campaigns` | `POST /adsApi/v1/create/campaigns` |
| Update Campaign | `PUT /sp/campaigns` | `POST /adsApi/v1/update/campaigns` |
| List Campaigns | `POST /sp/campaigns/list` | `POST /adsApi/v1/query/campaigns` |
| Delete Campaigns | `POST /sp/campaigns/delete` | `POST /adsApi/v1/delete/campaigns` |
| Create Ad Group | `POST /sp/adGroups` | `POST /adsApi/v1/create/adGroups` |
| Update Ad Group | `PUT /sp/adGroups` | `POST /adsApi/v1/update/adGroups` |
| List Ad Groups | `POST /sp/adGroups/list` | `POST /adsApi/v1/query/adGroups` |
| Delete Ad Groups | `POST /sp/adGroups/delete` | `POST /adsApi/v1/delete/adGroups` |
| Create Product Ad | `POST /sp/productAds` | `POST /adsApi/v1/create/ads` |
| Update Product Ad | `PUT /sp/productAds` | `POST /adsApi/v1/update/ads` |
| List Product Ads | `POST /sp/productAds/list` | `POST /adsApi/v1/query/ads` |
| Delete Product Ads | `POST /sp/productAds/delete` | `POST /adsApi/v1/delete/ads` |

### Targeting (Consolidated in Unified API)

| Operation | SP v3 API | Unified API |
|-----------|-----------|-------------|
| Create Keywords | `POST /sp/keywords` | `POST /adsApi/v1/create/targets` (targetType: KEYWORD) |
| Update Keywords | `PUT /sp/keywords` | `POST /adsApi/v1/update/targets` |
| List Keywords | `POST /sp/keywords/list` | `POST /adsApi/v1/query/targets` |
| Delete Keywords | `POST /sp/keywords/delete` | `POST /adsApi/v1/delete/targets` |
| Create Negative Keywords | `POST /sp/negativeKeywords` | `POST /adsApi/v1/create/targets` (negative: true) |
| Update Negative Keywords | `PUT /sp/negativeKeywords` | `POST /adsApi/v1/update/targets` |
| List Negative Keywords | `POST /sp/negativeKeywords/list` | `POST /adsApi/v1/query/targets` (negativeFilter) |
| Delete Negative Keywords | `POST /sp/negativeKeywords/delete` | `POST /adsApi/v1/delete/targets` |
| Create Targets | `POST /sp/targets` | `POST /adsApi/v1/create/targets` (targetType: PRODUCT/PRODUCT_CATEGORY) |
| Update Targets | `PUT /sp/targets` | `POST /adsApi/v1/update/targets` |
| List Targets | `POST /sp/targets/list` | `POST /adsApi/v1/query/targets` |
| Delete Targets | `POST /sp/targets/delete` | `POST /adsApi/v1/delete/targets` |
| Create Negative Targets | `POST /sp/negativeTargets` | `POST /adsApi/v1/create/targets` (negative: true) |
| Create Campaign Neg Keywords | `POST /sp/campaignNegativeKeywords` | `POST /adsApi/v1/create/targets` (negative: true, campaignId) |
| Create Campaign Neg Targets | `POST /sp/campaignNegativeTargets` | `POST /adsApi/v1/create/targets` (negative: true, campaignId) |

### HTTP Method Changes

| Operation | SP v3 | Unified API |
|-----------|-------|-------------|
| Update Campaign | `PUT` | `POST` |
| Update Ad Group | `PUT` | `POST` |
| Update Product Ad | `PUT` | `POST` |
| Update Keywords | `PUT` | `POST` |
| Update Targets | `PUT` | `POST` |

The Unified API uses `POST` for all operations.

---

## 4. Campaign-Level Field Mapping

### 4.1 Campaign Creation — Side by Side

**SP v3 API** (`POST /sp/campaigns`):
```json
{
  "campaigns": [{
    "name": "My SP Campaign",
    "state": "ENABLED",
    "budget": { "budget": 50.00, "budgetType": "DAILY" },
    "startDate": "2026-02-01",
    "dynamicBidding": {
      "strategy": "LEGACY_FOR_SALES",
      "placementBidding": [
        { "placement": "PLACEMENT_TOP", "percentage": 50 },
        { "placement": "PLACEMENT_PRODUCT_PAGE", "percentage": 25 }
      ]
    },
    "portfolioId": "<portfolio-id>"
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/campaigns`):
```json
{
  "campaigns": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "name": "My SP Campaign",
    "state": "ENABLED",
    "marketplaceScope": "SINGLE_MARKETPLACE",
    "startDateTime": "2026-02-01T00:00:00Z",
    "budgets": [{
      "budgetType": "MONETARY",
      "budgetValue": {
        "monetaryBudgetValue": {
          "monetaryBudget": { "value": 50.00 }
        }
      },
      "recurrenceTimePeriod": "DAILY"
    }],
    "optimizations": {
      "bidSettings": {
        "bidStrategy": "SALES_DOWN_ONLY",
        "bidAdjustments": {
          "placementBidAdjustments": [
            { "placement": "TOP_OF_SEARCH", "percentage": 50 },
            { "placement": "PRODUCT_PAGE", "percentage": 25 }
          ]
        }
      }
    },
    "portfolioId": "<portfolio-id>"
  }]
}
```

### 4.2 Field-by-Field Mapping

| Field | SP v3 | Unified API | Confidence |
|-------|-------|-------------|------------|
| Ad product | Not needed (implicit) | `adProduct: "SPONSORED_PRODUCTS"` (required) | ✅ Confirmed |
| Budget | `budget: { budget: 50, budgetType: "DAILY" }` | Nested `budgets[].budgetValue.monetaryBudgetValue.monetaryBudget.value` | ✅ Confirmed |
| Budget type | `budget.budgetType: "DAILY"` | `budgets[].recurrenceTimePeriod: "DAILY"` | ✅ Confirmed |
| Date format | `YYYY-MM-DD` | ISO 8601 `YYYY-MM-DDTHH:mm:ssZ` | ✅ Confirmed |
| Date fields | `startDate` / `endDate` | `startDateTime` / `endDateTime` | ✅ Confirmed |
| Marketplace | Not in body | `marketplaceScope: "SINGLE_MARKETPLACE"` (required) | ✅ Confirmed |
| Bidding structure | `dynamicBidding.strategy` + `placementBidding` | `optimizations.bidSettings.bidStrategy` + `bidAdjustments.placementBidAdjustments` | ✅ Confirmed |
| State values | `ENABLED`, `PAUSED`, `PROPOSED` | `ENABLED`, `PAUSED` (create); + `ARCHIVED` (read) | ✅ Confirmed |

### 4.3 Bidding Strategy Mapping

From the official enum mapping document:

| SP v3 `dynamicBidding.strategy` | Unified API `bidStrategy` | Confidence |
|----------------------------------|--------------------------|------------|
| `LEGACY_FOR_SALES` (down only) | `SALES_DOWN_ONLY` | ✅ Confirmed from enum doc |
| `AUTO_FOR_SALES` (up and down) | `SALES_UP_AND_DOWN` | ✅ Confirmed from enum doc |
| `MANUAL` (fixed bid) | `MANUAL` | ✅ Confirmed (identical) |
| `RULE_BASED` | `RULE_BASED` | ✅ Confirmed (identical) |

> Note: SB also maps `MAXIMIZE_IMMEDIATE_SALES` (v4 bidOptimization) → `SALES_UP_AND_DOWN`, and has a `NEW_TO_BRAND` → `MAXIMIZE_NEW_TO_BRAND_CUSTOMERS` mapping. SP does not use these.

### 4.4 Placement Enum Mapping

From the official enum mapping document:

| Unified API `placement` | SP v3 `placement` | SB v4 `placement` | Confidence |
|--------------------------|-------------------|--------------------|------------|
| `TOP_OF_SEARCH` | `PLACEMENT_TOP` | `TOP_OF_SEARCH` | ✅ Confirmed from enum doc |
| `PRODUCT_PAGE` | `PLACEMENT_PRODUCT_PAGE` | `DETAIL_PAGE` | ✅ Confirmed from enum doc |
| `REST_OF_SEARCH` | `REST_OF_SEARCH` | `REST_OF_SEARCH` | ✅ Confirmed from enum doc |
| `HOME_PAGE` | N/A (SP doesn't use) | `HOME` | ✅ Confirmed from enum doc |

---

## 5. Ad Group Field Mapping

**SP v3 API** (`POST /sp/adGroups`):
```json
{
  "adGroups": [{
    "campaignId": "<campaign-id>",
    "name": "My Ad Group",
    "state": "ENABLED",
    "defaultBid": 1.50
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/adGroups`):
```json
{
  "adGroups": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "campaignId": "<campaign-id>",
    "name": "My Ad Group",
    "state": "ENABLED",
    "bid": { "bid": 1.50 }
  }]
}
```

| Field | SP v3 | Unified API | Confidence |
|-------|-------|-------------|------------|
| Ad product | Not needed | `adProduct: "SPONSORED_PRODUCTS"` (required) | ✅ Confirmed |
| Default bid | `defaultBid: 1.50` (flat number) | `bid: { bid: 1.50 }` (object) | ✅ Confirmed |

---

## 6. Product Ad Field Mapping

**SP v3 API** (`POST /sp/productAds`):
```json
{
  "productAds": [{
    "campaignId": "<campaign-id>",
    "adGroupId": "<ad-group-id>",
    "state": "ENABLED",
    "asin": "B0XXXXXXX1",
    "sku": "SKU-123"
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/ads`):
```json
{
  "ads": [{
    "adGroupId": "<ad-group-id>",
    "adProduct": "SPONSORED_PRODUCTS",
    "adType": "PRODUCT_AD",
    "state": "ENABLED",
    "creative": {
      "productCreative": {
        "productCreativeSettings": {
          "advertisedProduct": {
            "productId": "B0XXXXXXX1",
            "productIdType": "ASIN"
          }
        }
      }
    }
  }]
}
```

| Field | SP v3 | Unified API | Confidence |
|-------|-------|-------------|------------|
| Wrapper | `productAds` array | `ads` array | ✅ Confirmed |
| Ad product | Not needed | `adProduct: "SPONSORED_PRODUCTS"` (required) | ✅ Confirmed |
| Ad type | Not needed | `adType: "PRODUCT_AD"` (required) | ✅ Confirmed |
| Campaign ID | `campaignId` in body | Not in ad body (inherited from ad group) | ✅ Confirmed |
| ASIN | `asin: "B0XX"` (flat string) | `creative.productCreative.productCreativeSettings.advertisedProduct.productId` + `productIdType: "ASIN"` | ✅ Confirmed |
| SKU | `sku: "SKU-123"` (flat string) | ❓ Check if SKU maps to productId with different productIdType | ❓ Unconfirmed |

> ❓ The SKU field mapping is not clear from the schema. The Unified API uses `productIdType: "ASIN"` — it's unclear whether SKU-based ads use a different `productIdType` or a different mechanism. Open a support case if you use SKU-based product ads.

---

## 7. Targeting Migration

This is the **most significant structural change**. SP v3 has separate endpoints for keywords, targets, negative keywords, negative targets, and campaign-level negatives. The Unified API consolidates ALL of these into a single `/adsApi/v1/create/targets` endpoint.

### 7.1 Keyword Target

**SP v3 API** (`POST /sp/keywords`):
```json
{
  "keywords": [{
    "campaignId": "<campaign-id>",
    "adGroupId": "<ad-group-id>",
    "state": "ENABLED",
    "keywordText": "women running shoes",
    "matchType": "BROAD",
    "bid": 1.50
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/targets`):
```json
{
  "targets": [{
    "adGroupId": "<ad-group-id>",
    "adProduct": "SPONSORED_PRODUCTS",
    "state": "ENABLED",
    "negative": false,
    "targetType": "KEYWORD",
    "bid": { "bid": 1.50 },
    "targetDetails": {
      "keywordTarget": {
        "keyword": "women running shoes",
        "matchType": "BROAD"
      }
    }
  }]
}
```

| Field | SP v3 | Unified API |
|-------|-------|-------------|
| Keyword text | `keywordText` | `targetDetails.keywordTarget.keyword` |
| Match type | `matchType` | `targetDetails.keywordTarget.matchType` |
| Bid | `bid: 1.50` (flat number) | `bid: { bid: 1.50 }` (object) |
| Campaign ID | `campaignId` (required) | Not needed (inherited from ad group) |
| Negative flag | Separate endpoint `/sp/negativeKeywords` | `negative: true` on same endpoint |

### 7.2 Negative Keyword

**SP v3 API** (`POST /sp/negativeKeywords`):
```json
{
  "negativeKeywords": [{
    "campaignId": "<campaign-id>",
    "adGroupId": "<ad-group-id>",
    "state": "ENABLED",
    "keywordText": "free",
    "matchType": "EXACT"
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/targets`):
```json
{
  "targets": [{
    "adGroupId": "<ad-group-id>",
    "adProduct": "SPONSORED_PRODUCTS",
    "state": "ENABLED",
    "negative": true,
    "targetType": "KEYWORD",
    "targetDetails": {
      "keywordTarget": {
        "keyword": "free",
        "matchType": "EXACT"
      }
    }
  }]
}
```

### 7.3 Product/Category Targeting

**SP v3 API** (`POST /sp/targets`) uses expression-based targeting. **Unified API** uses typed target objects:

| SP v3 Target Type | Unified API `targetType` | Unified API `targetDetails` |
|-------------------|--------------------------|----------------------------|
| ASIN targeting | `PRODUCT` | `productTarget: { product: { productId }, productIdType: "ASIN", matchType: "PRODUCT_EXACT" }` |
| Category targeting | `PRODUCT_CATEGORY` | `productCategoryTarget: { productCategoryRefinement: {...} }` |
| Auto targeting | `THEME` | `themeTarget: { matchType: "..." }` |

### 7.4 Product Match Type Mapping (ASIN Exact vs Expanded)

From the official enum mapping document — **this is how to distinguish ASIN exact vs expanded:**

| Unified API `matchType` | SP v3 expression | Description | Confidence |
|-------------------------|-----------------|-------------|------------|
| `PRODUCT_EXACT` | `ASIN_SAME_AS` | ASIN exact targeting (only the specified ASIN) | ✅ Confirmed from enum doc |
| `PRODUCT_SIMILAR` | `ASIN_EXPANDED_FROM` | ASIN expanded targeting (similar products) | ✅ Confirmed from enum doc |

**This is the answer to "how to distinguish ASIN exact vs expanded":**
- `targetType: "PRODUCT"` + `productTarget.matchType: "PRODUCT_EXACT"` = ASIN exact (精准)
- `targetType: "PRODUCT"` + `productTarget.matchType: "PRODUCT_SIMILAR"` = ASIN expanded (扩展)
- `targetType: "PRODUCT_CATEGORY"` = Category targeting (类目)

> Note: The Unified API OpenAPI schema (`UnifiedAPISP.json`) only lists `PRODUCT_EXACT` in the `SPProductMatchType` enum. However, the official enum mapping document confirms `PRODUCT_SIMILAR` also exists for expanded ASIN targeting. The schema may be incomplete.

### 7.4 Campaign-Level Negative Targeting

In SP v3, campaign-level negatives use separate endpoints (`/sp/campaignNegativeKeywords`, `/sp/campaignNegativeTargets`).

In the Unified API, use the same `/adsApi/v1/create/targets` endpoint with:
- `negative: true`
- `campaignId` instead of `adGroupId`
- Target level becomes campaign-level

### 7.5 Auto Targeting (Theme Targets)

SP v3 auto campaigns implicitly create auto targets. In the Unified API, these are explicit `THEME` type targets with the following match types:

**Auto target match types (✅ Confirmed from enum doc):**

| Unified API `matchType` | SP v3 equivalent | Description |
|-------------------------|-----------------|-------------|
| `SEARCH_LOOSE_MATCH` | `QUERY_BROAD_REL_MATCHES` | Loosely related search terms |
| `SEARCH_CLOSE_MATCH` | `QUERY_HIGH_REL_MATCHES` | Closely related search terms |
| `PRODUCT_SUBSTITUTES` | `ASIN_SUBSTITUTE_RELATED` | Similar product detail pages |
| `PRODUCT_COMPLEMENTS` | `ASIN_ACCESSORY_RELATED` | Complementary product detail pages |

**Theme target match types for manual SP campaigns (✅ Confirmed from enum doc):**

| Unified API `matchType` | SP v3 equivalent | Description |
|-------------------------|-----------------|-------------|
| `KEYWORDS_RELATED_TO_YOUR_BRAND` | `brand` | Keywords related to your brand |
| `KEYWORDS_RELATED_TO_GIFTS` | `gift` | Keywords related to gifts |
| `KEYWORDS_RELATED_TO_YOUR_PRODUCT_CATEGORY` | `category` | Keywords related to your product category |

### 7.6 Target Type Mapping

From the official enum mapping document, the Unified API `targetType` values include:

| Unified API `targetType` | Usage |
|--------------------------|-------|
| `AUTO` | Auto targeting (SP) |
| `KEYWORD` | Keyword targeting |
| `PRODUCT` | ASIN targeting |
| `PRODUCT_CATEGORY` | Category targeting |
| `PRODUCT_AUDIENCE` | Product audience (SD) |
| `PRODUCT_CATEGORY_AUDIENCE` | Category audience (SD) |
| `AUDIENCE` | Audience targeting (SD) |

### 7.7 Product ID Type

From the enum mapping document, `productIdType` supports:
- `ASIN` — ASIN identifier
- `SKU` — SKU identifier

This confirms that SKU-based product ads ARE supported in the Unified API via `productIdType: "SKU"`.

---

## 8. Audience Bid Adjustment Migration

**SP v3 API:**
```json
"dynamicBidding": {
  "shopperCohortBidding": [{
    "shopperCohortType": "AUDIENCE_SEGMENT",
    "percentage": 50,
    "audienceSegments": [{ "audienceId": "xxx", "audienceSegmentType": "SPONSORED_ADS_AMC" }]
  }]
}
```

**Unified API:**
```json
"optimizations": {
  "bidSettings": {
    "bidAdjustments": {
      "audienceBidAdjustments": [{ "audienceId": "xxx", "percentage": 50 }]
    }
  }
}
```

**What's confirmed (✅):**
- Unified API `audienceBidAdjustments` only has `audienceId` + `percentage`
- No `audienceSegmentType` or `shopperCohortType` fields exist in the Unified API

**Unconfirmed (❓):**
- Whether `audienceId` values are directly portable
- How "default audience" is expressed — open a support case to verify

---

## 9. Response Format Differences

**SP v3 success response:**
```json
{
  "campaigns": {
    "success": [{ "campaignId": "123", "index": 0 }],
    "error": [{ "index": 1, "errors": [...] }]
  }
}
```

**Unified API success response:**
```json
{
  "success": [{ "index": 0, "campaign": {...} }],
  "error": [{ "index": 1, "errors": [...] }]
}
```

Top-level `success`/`error` without a resource-type wrapper.

---

## 10. Features NOT Available in Unified API

The following v3 features must still use SP v3 endpoints:

- Budget rules — `/sp/budgetRules`
- Budget recommendations — `/sp/campaigns/budgetRecommendations`
- Budget usage — `/sp/campaigns/budget/usage`
- Campaign recommendations — `/sp/campaign/recommendations`
- Optimization rules — `/sp/rules/optimization`, `/sp/rules/campaignOptimization`
- Keyword recommendations — `/sp/targets/keywords/recommendations`
- Product recommendations — `/sp/targets/products/recommendations`
- Category recommendations — `/sp/targets/categories/recommendations`
- Targetable categories — `/sp/targets/categories`
- Category refinements — `/sp/targets/category/{id}/refinements`
- Bid recommendations — `/sp/targets/bid/recommendations`
- Negative target brand recommendations — `/sp/negativeTargets/brands/recommendations`
- Target promotion groups — `/sp/targetPromotionGroups`
- Events — `/sp/v1/events`

---

## 11. Migration Checklist

### Headers & Transport
- [ ] Rename `Amazon-Advertising-API-ClientId` → `Amazon-Ads-ClientId`
- [ ] Change Content-Type from versioned types to `application/json`
- [ ] Change all HTTP methods to `POST` (no more `PUT`)
- [ ] Update endpoint paths (`/sp/...` → `/adsApi/v1/...`)
- [ ] Remove `Prefer` header (not used in Unified API)

### Campaign Level
- [ ] Add `adProduct: "SPONSORED_PRODUCTS"` to all create/query requests
- [ ] Add `marketplaceScope: "SINGLE_MARKETPLACE"` to campaign creation
- [ ] Restructure budget from `{ budget, budgetType }` to nested `budgets[]` array
- [ ] Convert date strings from `YYYY-MM-DD` to ISO 8601 datetime
- [ ] Rename date fields (`startDate` → `startDateTime`)
- [ ] Move bidding from `dynamicBidding.strategy` to `optimizations.bidSettings.bidStrategy`
- [ ] Update bidding strategy enum (`LEGACY_FOR_SALES` → `SALES_DOWN_ONLY`, `AUTO_FOR_SALES` → `SALES_UP_AND_DOWN`)
- [ ] Update placement enums (`PLACEMENT_TOP` → `TOP_OF_SEARCH`, `PLACEMENT_PRODUCT_PAGE` → `PRODUCT_PAGE`, `PLACEMENT_REST_OF_SEARCH` → `REST_OF_SEARCH`)
- [ ] Restructure audience bid adjustments from `shopperCohortBidding` to `audienceBidAdjustments`

### Ad Group Level
- [ ] Add `adProduct: "SPONSORED_PRODUCTS"` to ad group requests
- [ ] Restructure `defaultBid` (number) to `bid: { bid: value }` (object)

### Ad Level
- [ ] Replace `/sp/productAds` with `/adsApi/v1/create/ads`
- [ ] Add `adType: "PRODUCT_AD"` to ad creation
- [ ] Wrap ASIN in `creative.productCreative.productCreativeSettings.advertisedProduct`
- [ ] Remove `campaignId` from ad body (inherited from ad group)

### Targeting (Major Change)
- [ ] Consolidate all keyword/target/negative endpoints to single `/adsApi/v1/create/targets`
- [ ] Add `targetType` field (KEYWORD, PRODUCT, PRODUCT_CATEGORY, THEME, LOCATION)
- [ ] Add `negative: true/false` flag (replaces separate negative endpoints)
- [ ] Move `keywordText` to `targetDetails.keywordTarget.keyword`
- [ ] Restructure bid from flat number to `bid: { bid: value }` object
- [ ] Remove `campaignId` from ad-group-level targets (use `adGroupId` only)
- [ ] For campaign-level negatives, use `campaignId` field instead of `adGroupId`

### Query/Response
- [ ] Add `adProductFilter` to all query requests
- [ ] Update response parsing — remove resource-type wrapper
- [ ] Update error handling for new `ErrorCode` enum

---

## 12. Common Migration Errors

**Problem: 400 Bad Request**
1. Missing `adProduct: "SPONSORED_PRODUCTS"`
2. Missing `marketplaceScope: "SINGLE_MARKETPLACE"`
3. Wrong date format (use `2026-01-15T00:00:00Z`, not `2026-01-15`)
4. Old header name `Amazon-Advertising-API-ClientId` instead of `Amazon-Ads-ClientId`
5. Versioned Content-Type instead of `application/json`
6. Budget as flat object instead of nested `budgets[]` array
7. Missing `adType: "PRODUCT_AD"` in ad creation
8. Missing `negative` field in target creation
9. Missing `targetType` field in target creation

**Problem: Placement bid adjustments rejected**
- Update enum values: `PLACEMENT_TOP` → `TOP_OF_SEARCH`, `PLACEMENT_PRODUCT_PAGE` → `PRODUCT_PAGE`

**Problem: Keyword creation fails**
- Use `/adsApi/v1/create/targets` not `/adsApi/v1/create/keywords` (no such endpoint)
- Set `targetType: "KEYWORD"` and `negative: false`
- Move `keywordText` to `targetDetails.keywordTarget.keyword`

**Problem: Bidding strategy rejected**
- `LEGACY_FOR_SALES` does not exist in Unified API — use `SALES_DOWN_ONLY`
- `AUTO_FOR_SALES` does not exist — use `SALES_UP_AND_DOWN`

---

## 13. Frequently Asked Questions

**Q: Do I need to migrate everything at once?**
A: No. Both APIs work simultaneously. Migrate incrementally.

**Q: Will campaigns created in v3 be visible in Unified API?**
A: Yes. The Unified API can query resources regardless of which API created them.

**Q: The biggest change seems to be targeting — why?**
A: SP v3 had 5+ separate endpoints for different target types. The Unified API consolidates them into one endpoint with `targetType` and `negative` fields. This is simpler long-term but requires restructuring your integration.

**Q: What happened to `campaignId` in keyword/target creation?**
A: In the Unified API, ad-group-level targets only need `adGroupId` — the campaign association is inherited. Only campaign-level targets (campaign negatives) need `campaignId`.

**Q: Is the `PROPOSED` state supported in Unified API?**
A: The Unified API create state only supports `ENABLED` and `PAUSED`. The `PROPOSED` state from v3 is not available for creation in the Unified API.

**Q: What about `autoManageCampaign` from v3?**
A: The Unified API has `autoCreationSettings.autoCreateTargets` which may serve a similar purpose. However, exact equivalence is ❓ **unconfirmed** — open a support case if you rely on this feature.

**Q: Is reporting affected?**
A: No. Reporting APIs are separate and unchanged.

---

## 14. Batch Size Limits

| Resource | Create | Query (maxResults) | Update | Delete |
|----------|--------|-------------------|--------|--------|
| Campaigns | 10 | 100 | 10 | 10 |
| Ad Groups | 10 | 100 | 10 | 10 |
| Ads | 10 | 100 | 10 | 10 |
| Targets | 1000 | 5000 | 1000 | 1000 |
