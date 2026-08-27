---
name: unified-sb-migration
description: "Use when migrating Sponsored Brands campaigns from SB v4 API (/sb/v4/) to the Unified API (/adsApi/v1/), including endpoint mapping, request body restructuring, header changes, field mapping differences, goal/bidding/audience migration, and troubleshooting migration errors."
---

# Sponsored Brands: SB v4 API to Unified API Migration Guide

## 1. Migration Overview

This guide helps API developers migrate from the **Sponsored Brands v4 API** (`sponsored-brands-4-openapi.json`, paths like `/sb/v4/...`) to the **Unified API** (`AmazonAdsAPISBMerged_prod_3p.json`, paths like `/adsApi/v1/...`).

### Key Architectural Changes

| Aspect | SB v4 API | Unified API |
|--------|-----------|-------------|
| Scope | SB-only API | Shared across all ad products (SB, SP, etc.) |
| Endpoint pattern | `/sb/v4/{resource}` | `/adsApi/v1/{action}/{resource}` |
| HTTP methods | Mixed (POST, PUT, GET, DELETE) | All `POST` |
| Content-Type | Versioned per resource | Standard `application/json` |
| Ad type routing | Separate endpoint per ad type | Single endpoint, type in request body |
| Ad product identifier | Implicit (from path) | Explicit `adProduct` field required |

### Can I Use Both APIs Simultaneously?

Yes. Existing v4 endpoints continue to work. You can migrate gradually — for example, use the Unified API for new SBC campaigns while keeping v4 for budget rules, insights, and other features not yet available in the Unified API.

### Confidence Legend

This guide contains mappings derived from comparing two API schemas. Each mapping is labeled:

- ✅ **Confirmed from schema** — directly verifiable from API spec field names, types, and enum values
- ⚠️ **Reasonable inference** — based on semantic analysis, naming patterns, and functional equivalence; not from an official migration guide
- ❓ **Unconfirmed** — cannot be determined from schema alone; recommend opening a support case with Amazon Ads API team

---

## 2. Header Changes

| Header | SB v4 API | Unified API | Confidence |
|--------|-----------|-------------|------------|
| Client ID | `Amazon-Advertising-API-ClientId` | `Amazon-Ads-ClientId` | ✅ Confirmed |
| Profile/Scope | `Amazon-Advertising-API-Scope` (required) | `Amazon-Advertising-API-Scope` (optional) | ✅ Confirmed |
| Account ID | Not used | `Amazon-Ads-AccountId` (optional) | ✅ Confirmed |
| Content-Type | Versioned: `application/vnd.sbcampaignresource.v4+json`, `application/vnd.sbadresource.v4+json`, etc. | Standard: `application/json` | ✅ Confirmed |

**Important:** The v4 API uses **versioned content types** (different per resource type). The Unified API uses plain `application/json` for all requests.

---

## 3. Endpoint Path Mapping

| Operation | SB v4 API | Unified API |
|-----------|-----------|-------------|
| Create Campaign | `POST /sb/v4/campaigns` | `POST /adsApi/v1/create/campaigns` |
| Update Campaign | `PUT /sb/v4/campaigns` | `POST /adsApi/v1/update/campaigns` |
| List Campaigns | `POST /sb/v4/campaigns/list` | `POST /adsApi/v1/query/campaigns` |
| Delete Campaigns | `POST /sb/v4/campaigns/delete` | `POST /adsApi/v1/delete/campaigns` |
| Create Ad Group | `POST /sb/v4/adGroups` | `POST /adsApi/v1/create/adGroups` |
| Update Ad Group | `PUT /sb/v4/adGroups` | `POST /adsApi/v1/update/adGroups` |
| List Ad Groups | `POST /sb/v4/adGroups/list` | `POST /adsApi/v1/query/adGroups` |
| Delete Ad Groups | `POST /sb/v4/adGroups/delete` | `POST /adsApi/v1/delete/adGroups` |
| Create Auto Collection Ad | `POST /sb/v4/ads/autoCollection` | `POST /adsApi/v1/create/ads` |
| Create Manual Collection Ad | `POST /sb/v4/ads/manualCollection` | `POST /adsApi/v1/create/ads` |
| Create Product Collection Ad | `POST /sb/v4/ads/productCollection` | `POST /adsApi/v1/create/ads` |
| Create Store Spotlight Ad | `POST /sb/v4/ads/storeSpotlight` | `POST /adsApi/v1/create/ads` |
| Create Video Ad | `POST /sb/v4/ads/video` | `POST /adsApi/v1/create/ads` |
| Create Brand Video Ad | `POST /sb/v4/ads/brandVideo` | `POST /adsApi/v1/create/ads` |
| Update Ad Creative (Auto) | `POST /sb/ads/creatives/autoCollection` | `POST /adsApi/v1/update/ads` |
| Update Ad Creative (Manual) | `POST /sb/ads/creatives/manualCollection` | `POST /adsApi/v1/update/ads` |
| Update Ad State | `PUT /sb/v4/ads` | `POST /adsApi/v1/update/ads` |
| List Ads | `POST /sb/v4/ads/list` | `POST /adsApi/v1/query/ads` |
| Delete Ads | `POST /sb/v4/ads/delete` | `POST /adsApi/v1/delete/ads` |
| Create Targets | *(separate targeting API)* | `POST /adsApi/v1/create/targets` |
| List Targets | *(separate targeting API)* | `POST /adsApi/v1/query/targets` |
| Update Targets | *(separate targeting API)* | `POST /adsApi/v1/update/targets` |
| Delete Targets | *(separate targeting API)* | `POST /adsApi/v1/delete/targets` |

### HTTP Method Changes

| Operation | SB v4 | Unified API |
|-----------|-------|-------------|
| Update Campaign | `PUT` | `POST` |
| Update Ad Group | `PUT` | `POST` |
| Update Ad | `PUT` (state) + `POST` (creative) | `POST` (unified) |

The Unified API uses `POST` for all operations (create, query, update, delete).

### Key Design Difference

In v4, each ad type has a **dedicated creation endpoint**. In the Unified API, all ad types use a **single endpoint** (`/adsApi/v1/create/ads`) and the ad type is determined by the creative settings in the request body:

| v4 Endpoint | Unified API Creative Field |
|-------------|---------------------------|
| `/sb/v4/ads/autoCollection` | `creative.componentCreative.autoCollectionSettings` |
| `/sb/v4/ads/manualCollection` | `creative.componentCreative.manualCollectionSettings` |
| `/sb/v4/ads/productCollection` | `creative.componentCreative.productCollectionSettings` |
| `/sb/v4/ads/storeSpotlight` | `creative.componentCreative.storeSpotlightSettings` |
| `/sb/v4/ads/video` | `creative.componentCreative.productVideoSettings` |
| `/sb/v4/ads/brandVideo` | `creative.componentCreative.productVideoSettings` (with STORE landing page) |

---

## 4. Campaign-Level Field Mapping

### 4.1 Campaign Creation — Side by Side

**SB v4 API:**
```json
{
  "campaigns": [
    {
      "name": "My Campaign",
      "state": "ENABLED",
      "budget": 50.00,
      "budgetType": "DAILY",
      "startDate": "2026-02-01",
      "brandEntityId": "<brand-entity-id>",
      "goal": "PAGE_VISIT",
      "costType": "CPC",
      "bidding": {
        "bidOptimization": false,
        "bidAdjustmentsByPlacement": [
          { "placement": "TOP_OF_SEARCH", "percentage": 50 }
        ]
      }
    }
  ]
}
```

**Unified API:**
```json
{
  "campaigns": [
    {
      "adProduct": "SPONSORED_BRANDS",
      "name": "My Campaign",
      "state": "ENABLED",
      "costType": "CPC",
      "marketplaceScope": "SINGLE_MARKETPLACE",
      "startDateTime": "2026-02-01T00:00:00Z",
      "brandId": "<brand-id>",
      "budgets": [
        {
          "budgetType": "MONETARY",
          "budgetValue": {
            "monetaryBudgetValue": {
              "monetaryBudget": { "value": 50.00 }
            }
          },
          "recurrenceTimePeriod": "DAILY"
        }
      ],
      "optimizations": {
        "goalSettings": {
          "kpi": "CLICKS"
        },
        "bidSettings": {
          "bidStrategy": "MANUAL",
          "bidAdjustments": {
            "placementBidAdjustments": [
              { "placement": "TOP_OF_SEARCH", "percentage": 50 }
            ]
          }
        }
      }
    }
  ]
}
```

### 4.2 Field-by-Field Mapping

| Field | SB v4 | Unified API | Confidence |
|-------|-------|-------------|------------|
| Ad product identifier | Not needed (implicit from path) | `adProduct: "SPONSORED_BRANDS"` (required) | ✅ Confirmed |
| Budget | Flat `budget` (number) + `budgetType` | Nested `budgets[].budgetValue.monetaryBudgetValue.monetaryBudget.value` | ✅ Confirmed |
| Budget recurrence | `budgetType: "DAILY"` or `"LIFETIME"` | `recurrenceTimePeriod: "DAILY"` or `"LIFETIME"` | ✅ Confirmed |
| Date format | `YYYY-MM-DD` string | ISO 8601 datetime `YYYY-MM-DDTHH:mm:ssZ` | ✅ Confirmed |
| Date field names | `startDate` / `endDate` | `startDateTime` / `endDateTime` | ✅ Confirmed |
| Brand ID | `brandEntityId` | `brandId` | ✅ Confirmed |
| Marketplace | Not in campaign body | `marketplaceScope: "SINGLE_MARKETPLACE"` (required) | ✅ Confirmed |
| Cost type | `costType` (optional, defaults to CPC) | `costType` (required) | ✅ Confirmed |
| Bidding structure | `bidding.bidOptimization` + `bidAdjustmentsByPlacement` | `optimizations.bidSettings.bidStrategy` + `bidAdjustments.placementBidAdjustments` | ✅ Confirmed |
| Bid optimization toggle | `bidOptimization: true/false` | `bidStrategy: "SALES_UP_AND_DOWN"` / `"MANUAL"` | ✅ Confirmed from enum doc |
| Placement enums | `HOME`, `DETAIL_PAGE`, `OTHER`, `TOP_OF_SEARCH` | `HOME_PAGE`, `PRODUCT_PAGE`, `REST_OF_SEARCH`, `TOP_OF_SEARCH` | ✅ Confirmed from enum doc |
| Goal | `goal: "PAGE_VISIT"` / `"BRAND_IMPRESSION_SHARE"` | `optimizations.goalSettings.kpi` | ⚠️ Reasonable inference (see section 5) |

> ⚠️ Items marked "Reasonable inference" are based on semantic analysis of both API schemas. They are NOT from an official migration document. If any of these mappings cause issues, open a support case with the Amazon Ads API team.

---

## 5. Goal & Bidding Migration

### 5.1 Goal Mapping

**v4 `goal` values:** `PAGE_VISIT` (default), `BRAND_IMPRESSION_SHARE`
**Unified API `goalSettings.kpi` values:** `CLICKS`, `TOP_OF_SEARCH_IMPRESSION_SHARE`

**⚠️ Reasonable inference (based on semantic and costType correlation):**

| v4 `goal` | Unified API `goalSettings.kpi` | costType | Reasoning |
|-----------|-------------------------------|----------|-----------|
| `PAGE_VISIT` | `CLICKS` | `CPC` | Both target driving traffic/clicks |
| `BRAND_IMPRESSION_SHARE` | `TOP_OF_SEARCH_IMPRESSION_SHARE` | `VCPM` | Both target maximizing top-of-search impression share |

Additionally, the Unified API `goalSettings` response contains both `goal` (enum: `AWARENESS`, `CONSIDERATION`, `CONVERSIONS`) and `kpi`. The inferred full mapping:

| v4 goal | Unified API goalSettings.goal | Unified API goalSettings.kpi |
|---------|-------------------------------|------------------------------|
| `PAGE_VISIT` | `CONVERSIONS` | `CLICKS` |
| `BRAND_IMPRESSION_SHARE` | `AWARENESS` | `TOP_OF_SEARCH_IMPRESSION_SHARE` |

When creating campaigns via `SBCreateGoalSettings`, only `kpi` is required (not `goal`).

> ⚠️ **This mapping is a reasonable inference** based on semantic analysis of both API specs and the costType linkage. It is NOT explicitly documented in a migration guide. If you need official confirmation, **open a support case** with the Amazon Ads API support team.

### 5.2 Bid Strategy Mapping

| v4 `bidding` | Unified API `optimizations.bidSettings` | Confidence |
|-------------|----------------------------------------|------------|
| `bidOptimization: false` | `bidStrategy: "MANUAL"` | ⚠️ Reasonable inference |
| `bidOptimization: true` | `bidStrategy: "SALES_UP_AND_DOWN"` | ⚠️ Reasonable inference |

**Rationale:** v4 `bidOptimization: true` means "Amazon automatically adjusts bids." The Unified API `SALES_UP_AND_DOWN` description says "Increases or decreases your bids in real time by a maximum of 100%." Both represent automatic bid optimization.

### 5.3 Placement Bid Adjustment Mapping

| v4 `placement` enum | Unified API `placement` enum | Confidence |
|---------------------|------------------------------|------------|
| `TOP_OF_SEARCH` | `TOP_OF_SEARCH` | ✅ Confirmed (identical) |
| `HOME` | `HOME_PAGE` | ⚠️ Reasonable inference |
| `DETAIL_PAGE` | `PRODUCT_PAGE` | ⚠️ Reasonable inference |
| `OTHER` | `REST_OF_SEARCH` | ⚠️ Reasonable inference |

**v4 bid adjustment range:** -99 to 900
**Unified API bid adjustment range:** Specified per request (check current limits)

### 5.4 Audience Bid Adjustment Mapping

**v4 structure:**
```json
"bidding": {
  "shopperCohortBidAdjustments": [{
    "shopperCohortType": "AUDIENCE_SEGMENT",
    "percentage": 50,
    "audienceSegments": [{ "audienceId": "xxx", "audienceSegmentType": "SPONSORED_ADS_AMC" }]
  }]
}
```

**Unified API structure:**
```json
"optimizations": {
  "bidSettings": {
    "bidAdjustments": {
      "audienceBidAdjustments": [{ "audienceId": "xxx", "percentage": 50 }]
    }
  }
}
```

**What's confirmed from schema (✅):**
- Unified API `audienceBidAdjustments` only has two fields: `audienceId` (string, required) and `percentage` (int32, required)
- There is NO `audienceSegmentType` field in the Unified API schema
- There is NO `shopperCohortType` field in the Unified API schema
- The Unified API has a `shopperSegmentBidAdjustments` field but it is explicitly marked as **"Legacy SB field (marked for deprecation)"** — do NOT use it

**Reasonable inference (⚠️):**
- `shopperCohortType: "AUDIENCE_SEGMENT"` is implicitly fixed — no need to pass it
- `audienceSegmentType` (SPONSORED_ADS_AMC / BEHAVIOR_DYNAMIC) distinction is likely handled server-side based on the `audienceId` itself

**Unconfirmed (❓ — open a support case):**
- How "default audience" is expressed in the Unified API — the schema does not reveal this
- Whether all `audienceId` values from v4 work identically in the Unified API
- Whether there are any behavioral differences for SPONSORED_ADS_AMC vs BEHAVIOR_DYNAMIC audiences when the type is not explicitly provided

> **Recommendation:** Open a support case to confirm:
> 1. Whether `audienceId` values are directly portable from v4 to the Unified API
> 2. How "default audience" concept maps in the Unified API
> 3. Whether the removal of `audienceSegmentType` has any impact on bid adjustment behavior

---

## 6. Ad-Level Field Mapping

### 6.1 Auto Collection Ad

**SB v4 API** (`POST /sb/v4/ads/autoCollection`):
```json
{
  "ads": [{
    "adGroupId": "<id>",
    "name": "Auto Ad",
    "state": "ENABLED",
    "creative": {
      "brandName": "Your Brand",
      "brandLogoAssetID": "<asset-id>",
      "brandLogoCrop": { "top": 0, "left": 0, "width": 400, "height": 400 },
      "asinExclusions": ["B0XXXXXXX1"]
    }
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/ads`):
```json
{
  "ads": [{
    "adGroupId": "<id>",
    "adProduct": "SPONSORED_BRANDS",
    "adType": "COMPONENT",
    "name": "Auto Ad",
    "state": "ENABLED",
    "creative": {
      "componentCreative": {
        "autoCollectionSettings": {
          "sharedSettings": {
            "brand": "Your Brand",
            "brandLogos": {
              "assetId": "<asset-id>",
              "assetVersion": "<version>",
              "formatProperties": [{ "top": 0, "left": 0, "width": 400, "height": 400 }]
            }
          },
          "productExclusions": [
            { "productId": "B0XXXXXXX1", "productIdType": "ASIN" }
          ]
        }
      }
    }
  }]
}
```

| Field | SB v4 | Unified API |
|-------|-------|-------------|
| Ad product | Not needed | `adProduct: "SPONSORED_BRANDS"` (required) |
| Ad type | Not needed | `adType: "COMPONENT"` (required) |
| Brand name | `creative.brandName` | `creative.componentCreative.autoCollectionSettings.sharedSettings.brand` |
| Brand logo | `brandLogoAssetID` (string) | `brandLogos: { assetId, assetVersion }` (object) |
| Brand logo crop | `brandLogoCrop: { top, left, width, height }` | `brandLogos.formatProperties: [{ top, left, width, height }]` |
| ASIN exclusions | `asinExclusions: ["B0XX"]` (string array) | `productExclusions: [{ productId, productIdType }]` (object array) |

### 6.2 Manual Collection Ad

**SB v4 API** (`POST /sb/v4/ads/manualCollection`):
```json
{
  "ads": [{
    "adGroupId": "<id>",
    "name": "Manual Ad",
    "state": "ENABLED",
    "creative": {
      "brandName": "Your Brand",
      "asins": ["B0XXX1", "B0XXX2", "B0XXX3"],
      "landingPage": {
        "pageType": "PRODUCT_LIST",
        "url": "https://..."
      }
    }
  }]
}
```

**Unified API** (`POST /adsApi/v1/create/ads`):
```json
{
  "ads": [{
    "adGroupId": "<id>",
    "adProduct": "SPONSORED_BRANDS",
    "adType": "COMPONENT",
    "name": "Manual Ad",
    "state": "ENABLED",
    "creative": {
      "componentCreative": {
        "manualCollectionSettings": {
          "sharedSettings": {
            "brand": "Your Brand"
          },
          "productInclusions": [
            { "productId": "B0XXX1", "productIdType": "ASIN" },
            { "productId": "B0XXX2", "productIdType": "ASIN" },
            { "productId": "B0XXX3", "productIdType": "ASIN" }
          ],
          "landingPage": {
            "landingPageType": "ASIN_LIST",
            "landingPageUrl": "https://..."
          }
        }
      }
    }
  }]
}
```

| Field | SB v4 | Unified API |
|-------|-------|-------------|
| Products | `asins: ["B0XX"]` (string array) | `productInclusions: [{ productId, productIdType }]` (object array) |
| Landing page type | `pageType: "PRODUCT_LIST"` / `"STORE"` | `landingPageType: "ASIN_LIST"` / `"STORE"` |
| Landing page URL | `landingPage.url` | `landingPage.landingPageUrl` |

### 6.3 Ad Update / Creative Revision

**SB v4 API** has separate flows:
- State/name update: `PUT /sb/v4/ads` with `{ ads: [{ adId, state }] }`
- Creative revision: `POST /sb/ads/creatives/autoCollection` or `/manualCollection`

**Unified API** unifies both into a single endpoint:
- `POST /adsApi/v1/update/ads` with `{ ads: [{ adId, state, creative: {...} }] }`

---

## 7. Query/List Differences

**SB v4 API** (`POST /sb/v4/ads/list`):
```json
{
  "campaignIdFilter": { "include": ["<id>"] },
  "stateFilter": { "include": ["ENABLED", "PAUSED"] },
  "maxResults": 100,
  "nextToken": "..."
}
```

**Unified API** (`POST /adsApi/v1/query/ads`):
```json
{
  "adProductFilter": { "include": ["SPONSORED_BRANDS"] },
  "campaignIdFilter": { "include": ["<id>"] },
  "stateFilter": { "include": ["ENABLED", "PAUSED"] },
  "maxResults": 100,
  "nextToken": "..."
}
```

**Key difference:** The Unified API requires `adProductFilter` in all query requests since the endpoint is shared across ad products.

---

## 8. Response Format Differences

**SB v4 success response:**
```json
{
  "campaigns": {
    "success": [
      { "campaignId": "123", "index": 0, "campaign": {...} }
    ],
    "error": [
      { "index": 1, "errors": [...] }
    ]
  }
}
```

**Unified API success response:**
```json
{
  "success": [
    { "index": 0, "campaign": {...} }
  ],
  "error": [
    { "index": 1, "errors": [...] }
  ]
}
```

The Unified API returns `success`/`error` at the **top level** without the resource-type wrapper.

### Error Code Differences

v4 uses exception-based errors: `InvalidArgumentException`, `AccessDeniedException`, `ThrottlingException`

Unified API uses a standardized `ErrorCode` enum:

| Scenario | v4 Error | Unified API Error Code |
|----------|----------|----------------------|
| Bad request | `InvalidArgumentException` (400) | `BAD_REQUEST` or specific field-level codes |
| Auth failed | `UnauthorizedException` (401) | `UNAUTHORIZED` |
| No access | `AccessDeniedException` (403) | `FORBIDDEN` |
| Rate limited | `ThrottlingException` (429) | `TOO_MANY_REQUESTS` |
| Server error | `InternalServerException` (500) | `INTERNAL_ERROR` |

---

## 9. Features NOT Available in Unified API

The following v4 features are not in the Unified API spec and must still use v4 endpoints:

- Budget rules — `POST/PUT/GET /sb/budgetRules`
- Budget recommendations — `POST /sb/campaigns/budgetRecommendations`
- Budget usage — `POST /sb/campaigns/budget/usage`
- Campaign insights — `POST /sb/campaigns/insights`
- Forecasts — `POST /sb/forecasts`
- Optimization rules — `POST/PUT /sb/rules/optimization`
- Headline recommendations — `POST /sb/recommendations/creative/headline`
- Negative target brand recommendations — `GET /sb/negativeTargets/brands/recommendations`
- Product targeting categories — `GET /sb/targets/categories`
- Category refinements — `GET /sb/targets/categories/{id}/refinements`
- V3 campaign migration — `POST /sb/v4/legacyCampaigns/migrationJob`

---

## 10. Migration Checklist

### Headers & Transport
- [ ] Rename `Amazon-Advertising-API-ClientId` → `Amazon-Ads-ClientId`
- [ ] Change Content-Type from versioned types to `application/json`
- [ ] Change all HTTP methods to `POST` (no more `PUT`)
- [ ] Update endpoint paths (`/sb/v4/...` → `/adsApi/v1/...`)

### Campaign Level
- [ ] Add `adProduct: "SPONSORED_BRANDS"` to all create/query requests
- [ ] Add `marketplaceScope: "SINGLE_MARKETPLACE"` to campaign creation
- [ ] Add `costType` (now required, not optional)
- [ ] Restructure budget from flat `budget` + `budgetType` to nested `budgets[]` array
- [ ] Convert date strings from `YYYY-MM-DD` to ISO 8601 datetime
- [ ] Rename date fields (`startDate` → `startDateTime`, `endDate` → `endDateTime`)
- [ ] Rename `brandEntityId` → `brandId`
- [ ] Move goal from `goal` to `optimizations.goalSettings.kpi`
- [ ] Restructure bidding from `bidding.bidOptimization` to `optimizations.bidSettings.bidStrategy`
- [ ] Update placement enums (`HOME` → `HOME_PAGE`, `DETAIL_PAGE` → `PRODUCT_PAGE`, `OTHER` → `REST_OF_SEARCH`)
- [ ] Restructure audience bid adjustments from `shopperCohortBidAdjustments` to `audienceBidAdjustments`

### Ad Level
- [ ] Add `adType: "COMPONENT"` to ad creation requests
- [ ] Route all ad types through single `/adsApi/v1/create/ads` endpoint
- [ ] Wrap creative in `componentCreative.{settingsType}`
- [ ] Move `brandName` to `sharedSettings.brand`
- [ ] Convert `brandLogoAssetID` (string) to `brandLogos: { assetId, assetVersion }` (object)
- [ ] Convert `asins` string arrays to `productInclusions` object arrays with `productIdType`
- [ ] Convert `asinExclusions` string arrays to `productExclusions` object arrays
- [ ] Update landing page type enum (`PRODUCT_LIST` → `ASIN_LIST`)
- [ ] Rename `landingPage.url` → `landingPage.landingPageUrl`

### Query/Response
- [ ] Add `adProductFilter` to all query/list requests
- [ ] Update response parsing — remove resource-type wrapper (top-level `success`/`error`)
- [ ] Update error handling for new `ErrorCode` enum

---

## 11. Common Migration Errors

**Problem: 400 Bad Request after switching to Unified API**

Check these common causes:
1. Missing `adProduct: "SPONSORED_BRANDS"` in request body
2. Missing `marketplaceScope: "SINGLE_MARKETPLACE"` in campaign creation
3. Wrong date format — use ISO 8601 `2026-01-15T00:00:00Z`, not `2026-01-15`
4. Using old header name `Amazon-Advertising-API-ClientId` instead of `Amazon-Ads-ClientId`
5. Using wrong Content-Type — should be `application/json`, not versioned types
6. Budget structured as flat number instead of nested `budgets[]` array
7. Missing `adType: "COMPONENT"` in ad creation
8. Missing `costType` field (now required)

**Problem: Placement bid adjustments not working**

- Check that you updated the enum values: `HOME` → `HOME_PAGE`, `DETAIL_PAGE` → `PRODUCT_PAGE`, `OTHER` → `REST_OF_SEARCH`
- Placement adjustments are NOT supported with `bidStrategy: "SALES_UP_AND_DOWN"`

**Problem: Ad creation fails with `FIELD_VALUE_IS_INVALID`**

- Verify you are using the correct creative wrapper for your ad type
- For collections: use `componentCreative.autoCollectionSettings` or `manualCollectionSettings`
- Ensure `productIdType: "ASIN"` is included in all product objects

---

## 12. Frequently Asked Questions

**Q: Do I need to migrate everything at once?**
A: No. Both APIs work simultaneously. Migrate one resource type at a time if needed.

**Q: Will campaigns created in v4 be visible in Unified API?**
A: Yes. The Unified API can query campaigns regardless of which API was used to create them.

**Q: What about targeting? v4 has a separate targeting API.**
A: The Unified API includes full targeting CRUD at `/adsApi/v1/create/targets`, `/query/targets`, `/update/targets`, `/delete/targets`. Target types include KEYWORD, PRODUCT, PRODUCT_CATEGORY, and THEME.

**Q: I used `smartDefault: ["TARGETING"]` in v4. What's the equivalent?**
A: The Unified API has `autoCreationSettings.autoCreateTargets: true` on campaigns which serves a similar purpose. However, the exact behavioral equivalence is ❓ **unconfirmed** — open a support case if you rely on this feature.

**Q: My v4 integration uses budget rules and optimization rules. Can I migrate those?**
A: No. Budget rules (`/sb/budgetRules`) and optimization rules (`/sb/rules/optimization`) are not available in the Unified API. Continue using v4 endpoints for those features.

**Q: The Unified API has `ARCHIVED` state but v4 delete uses a separate endpoint. Which should I use?**
A: The Unified API has both — dedicated delete endpoints (`/adsApi/v1/delete/campaigns`) and the ability to set state to `ARCHIVED` via update. The delete endpoint archives the resource. Both approaches work.

**Q: Is reporting affected by this migration?**
A: No. Reporting APIs are separate and unchanged. Continue using existing Sponsored Brands reporting endpoints.

---

## 13. Batch Size Limits

| Resource | Create | Query (maxResults) | Update | Delete |
|----------|--------|-------------------|--------|--------|
| Campaigns | 10 | 100 | 10 | 10 |
| Ad Groups | 10 | 100 | 10 | 10 |
| Ads | 10 | 100 | 10 | 10 |
| Targets | 1000 | 5000 | 1000 | 1000 |
| Ad Extensions | 50 | 1000 | 50 | — |
