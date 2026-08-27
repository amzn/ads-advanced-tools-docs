---
name: unified-api-migration-guide
description: "Use when a developer asks about migrating from legacy Amazon Ads APIs (SP v3 or SB v4) to the Unified API (/adsApi/v1/), assessing migration scope, identifying effort levels, determining what can and cannot be migrated, and planning migration steps."
---

# Amazon Ads: Legacy API to Unified API Migration Guide

## How to Use This Skill

This skill guides developers through a structured migration assessment. When a developer asks about migration, walk them through these steps in order. For detailed field-level mapping, refer to the product-specific skills:
- **SP migration details** → `unified-sp-migration` skill
- **SB migration details** → `unified-sb-migration` skill

---

## Step 1: Discovery — What Are You Currently Using?

Ask the developer to identify their current integration:

### Questions to Ask

1. **Which ad product(s)?** SP, SB, or both?
2. **Which endpoints do you call?** (provide the checklist below)
3. **Which operations?** Create, Read/List, Update, Delete, Recommendations, Reporting?
4. **What's your priority?** New feature adoption? Sunset deadline? Consolidation?

### SP v3 Endpoint Checklist

| Category | Endpoints | Migrable? |
|----------|-----------|-----------|
| Campaigns | `/sp/campaigns` (CRUD) | ✅ Yes |
| Ad Groups | `/sp/adGroups` (CRUD) | ✅ Yes |
| Product Ads | `/sp/productAds` (CRUD) | ✅ Yes |
| Keywords | `/sp/keywords` (CRUD) | ✅ Yes (→ targets) |
| Negative Keywords | `/sp/negativeKeywords` (CRUD) | ✅ Yes (→ targets, negative=true) |
| Campaign Negative Keywords | `/sp/campaignNegativeKeywords` (CRUD) | ✅ Yes (→ targets, campaign-level) |
| Targets | `/sp/targets` (CRUD) | ✅ Yes |
| Negative Targets | `/sp/negativeTargets` (CRUD) | ✅ Yes (→ targets, negative=true) |
| Campaign Negative Targets | `/sp/campaignNegativeTargets` (CRUD) | ✅ Yes (→ targets, campaign-level) |
| Budget Rules | `/sp/budgetRules` | ❌ No — keep using v3 |
| Budget Recommendations | `/sp/campaigns/budgetRecommendations` | ❌ No |
| Budget Usage | `/sp/campaigns/budget/usage` | ❌ No |
| Campaign Recommendations | `/sp/campaign/recommendations` | ❌ No |
| Optimization Rules | `/sp/rules/optimization` | ❌ No |
| Keyword Recommendations | `/sp/targets/keywords/recommendations` | ❌ No |
| Product Recommendations | `/sp/targets/products/recommendations` | ❌ No |
| Bid Recommendations | `/sp/targets/bid/recommendations` | ❌ No |
| Targetable Categories | `/sp/targets/categories` | ❌ No |
| Category Refinements | `/sp/targets/category/{id}/refinements` | ❌ No |
| Negative Target Brand Recs | `/sp/negativeTargets/brands/recommendations` | ❌ No |
| Target Promotion Groups | `/sp/targetPromotionGroups` | ❌ No |
| Events | `/sp/v1/events` | ❌ No |
| Reporting | Reporting APIs | ❌ No change needed (separate API) |

### SB v4 Endpoint Checklist

| Category | Endpoints | Migrable? |
|----------|-----------|-----------|
| Campaigns | `/sb/v4/campaigns` (CRUD) | ✅ Yes |
| Ad Groups | `/sb/v4/adGroups` (CRUD) | ✅ Yes |
| Ads (all types) | `/sb/v4/ads/*` (CRUD) | ✅ Yes |
| Ad Creatives | `/sb/ads/creatives/*` | ✅ Yes (merged into update/ads) |
| Budget Rules | `/sb/budgetRules` | ❌ No — keep using v4 |
| Budget Recommendations | `/sb/campaigns/budgetRecommendations` | ❌ No |
| Budget Usage | `/sb/campaigns/budget/usage` | ❌ No |
| Campaign Insights | `/sb/campaigns/insights` | ❌ No |
| Forecasts | `/sb/forecasts` | ❌ No |
| Optimization Rules | `/sb/rules/optimization` | ❌ No |
| Headline Recommendations | `/sb/recommendations/creative/headline` | ❌ No |
| Negative Target Brand Recs | `/sb/negativeTargets/brands/recommendations` | ❌ No |
| Targetable Categories | `/sb/targets/categories` | ❌ No |
| V3 Migration | `/sb/v4/legacyCampaigns/migrationJob` | ❌ No |
| Reporting | Reporting APIs | ❌ No change needed (separate API) |

---

## Step 2: Assess — Effort Level Per Area

Once you know what endpoints are in scope, assess the effort level:

### Effort Matrix

| Migration Area | Effort | Reason |
|---------------|--------|--------|
| **Headers** | 🟢 Low | Rename 1 header, change Content-Type |
| **Campaign CRUD** | 🟡 Medium | Budget restructuring, date format, bidding restructuring |
| **Ad Group CRUD** | 🟢 Low | Add adProduct, wrap bid in object |
| **Ad/Product Ad CRUD** | 🟡 Medium | New creative wrapper structure |
| **Targeting (SP)** | 🔴 High | 5+ endpoints consolidated into 1; complete restructuring |
| **Targeting (SB)** | 🟡 Medium | Similar consolidation but simpler structure |
| **Bidding/Placement** | 🟡 Medium | Enum renames, structure move |
| **Audience Adjustments** | 🟡 Medium | Flattened structure, fields removed |
| **Query/List** | 🟢 Low | Add adProductFilter, minor response changes |
| **Response Parsing** | 🟢 Low | Remove wrapper, update error handling |

### SP-Specific Effort Summary

| If you use... | Effort | Key changes |
|--------------|--------|-------------|
| Campaign CRUD only | 🟡 Medium | Budget, dates, bidding restructuring |
| + Ad Groups | 🟢 Low added | Wrap defaultBid in object |
| + Product Ads | 🟡 Medium added | New creative wrapper for ASIN |
| + Keywords | 🔴 High added | Move to /targets endpoint, new structure |
| + Targets (product/category) | 🔴 High added | Expression format → typed objects |
| + Negatives (all types) | 🔴 High added | Consolidate 4 endpoints into 1 with flags |
| + Budget Rules/Recommendations | No effort | Keep using v3 (not migrable) |

### SB-Specific Effort Summary

| If you use... | Effort | Key changes |
|--------------|--------|-------------|
| Campaign CRUD only | 🟡 Medium | Budget, dates, goal, bidding restructuring |
| + Ad Groups | 🟢 Low added | Add adProduct |
| + Ads (collections) | 🟡 Medium added | New componentCreative wrapper |
| + Ads (video/spotlight) | 🟡 Medium added | New creative settings structure |
| + Budget Rules/Insights | No effort | Keep using v4 (not migrable) |

---

## Step 3: Understand What Changes

### 3.1 Transport-Level Changes (applies to ALL endpoints)

| Change | Before | After | Impact |
|--------|--------|-------|--------|
| Client ID header | `Amazon-Advertising-API-ClientId` | `Amazon-Ads-ClientId` | Global find-replace |
| Content-Type | Versioned per resource | `application/json` | Global find-replace |
| HTTP method | Mixed (POST/PUT/GET/DELETE) | All `POST` | Update HTTP client calls |
| Scope header | Required | Optional | Can keep sending it |
| Account ID header | N/A | `Amazon-Ads-AccountId` (optional) | Optional addition |

### 3.2 Request Body Changes (applies to ALL create/query)

| Change | Before | After |
|--------|--------|-------|
| Ad product | Not needed | `adProduct: "SPONSORED_PRODUCTS"` or `"SPONSORED_BRANDS"` required |
| Marketplace | Not needed | `marketplaceScope: "SINGLE_MARKETPLACE"` required for campaigns |
| Date format | `YYYY-MM-DD` | ISO 8601 `YYYY-MM-DDTHH:mm:ssZ` |
| Date field names | `startDate`/`endDate` | `startDateTime`/`endDateTime` |
| Query filter | Optional | `adProductFilter` required in all queries |

### 3.3 Response Changes

| Change | Before | After |
|--------|--------|-------|
| Wrapper | `{ "campaigns": { "success": [...], "error": [...] } }` | `{ "success": [...], "error": [...] }` |
| Status field | `servingStatus` (single string, always present) | `status: { deliveryStatus, deliveryReasons[] }` (optional, array of reasons) |
| Error format | Exception-based (`InvalidArgumentException`) | Enum-based (`ErrorCode: "BAD_REQUEST"`) |

---

## Step 4: Key Enum Mappings (Quick Reference)

### Bidding Strategy

| SP v3 | SB v4 | Unified API |
|-------|-------|-------------|
| `LEGACY_FOR_SALES` | — | `SALES_DOWN_ONLY` |
| `AUTO_FOR_SALES` | `bidOptimization: true` | `SALES_UP_AND_DOWN` |
| `MANUAL` | `bidOptimization: false` | `MANUAL` |
| `RULE_BASED` | — | `RULE_BASED` |

### Placement

| Unified API | SP v3 | SB v4 |
|-------------|-------|-------|
| `TOP_OF_SEARCH` | `PLACEMENT_TOP` | `TOP_OF_SEARCH` |
| `PRODUCT_PAGE` | `PLACEMENT_PRODUCT_PAGE` | `DETAIL_PAGE` |
| `REST_OF_SEARCH` | `REST_OF_SEARCH` | `REST_OF_SEARCH` |
| `HOME_PAGE` | N/A | `HOME` |

### SP Auto Target Match Types

| Unified API | SP v3 |
|-------------|-------|
| `SEARCH_LOOSE_MATCH` | `QUERY_BROAD_REL_MATCHES` |
| `SEARCH_CLOSE_MATCH` | `QUERY_HIGH_REL_MATCHES` |
| `PRODUCT_SUBSTITUTES` | `ASIN_SUBSTITUTE_RELATED` |
| `PRODUCT_COMPLEMENTS` | `ASIN_ACCESSORY_RELATED` |

### SP Product Match Types

| Unified API | SP v3 | Meaning |
|-------------|-------|---------|
| `PRODUCT_EXACT` | `ASIN_SAME_AS` | ASIN exact |
| `PRODUCT_SIMILAR` | `ASIN_EXPANDED_FROM` | ASIN expanded |

### SB Landing Page Types

| Unified API | SB v4 |
|-------------|-------|
| `ASIN_LIST` | `PRODUCT_LIST` |
| `STORE` | `STORE` |

---

## Step 5: Migration Plan Template

Recommend this phased approach to developers:

### Phase 1: Transport Layer (Day 1)
- Update headers
- Change Content-Type to `application/json`
- Change all methods to POST
- Update base paths

### Phase 2: Read Operations (Week 1)
- Migrate query/list endpoints first (low risk, read-only)
- Add `adProductFilter` to all queries
- Update response parsing
- Validate data matches v3/v4 responses

### Phase 3: Campaign/Ad Group Write (Week 2)
- Migrate campaign create/update
- Migrate ad group create/update
- Test with real campaigns in sandbox/test account

### Phase 4: Ad & Targeting Write (Week 3-4)
- Migrate ad creation (new creative structure)
- Migrate targeting (biggest change for SP)
- Consolidate negative targeting endpoints

### Phase 5: Cleanup
- Remove old v3/v4 code paths
- Keep v3/v4 only for non-migrable features (budget rules, recommendations, etc.)
- Update error handling for new error codes

---

## Step 6: Common Pitfalls

| # | Pitfall | How to Avoid |
|---|---------|-------------|
| 1 | Forgetting `adProduct` field | Add to every create/query request body |
| 2 | Wrong date format | Use ISO 8601 with timezone, not YYYY-MM-DD |
| 3 | Old Content-Type | Global replace to `application/json` |
| 4 | Old header name | `Amazon-Advertising-API-ClientId` → `Amazon-Ads-ClientId` |
| 5 | Flat budget number | Must use nested `budgets[]` array |
| 6 | Missing `adType` on ads | Always include (SP: `PRODUCT_AD`, SB: `COMPONENT`) |
| 7 | Old placement enums | Check mapping table above |
| 8 | Old bidding strategy enums | `LEGACY_FOR_SALES` → `SALES_DOWN_ONLY` etc. |
| 9 | Separate negative endpoints | Use same `/targets` endpoint with `negative: true` |
| 10 | Expecting `servingStatus` always present | `status` is optional in Unified API |

---

## Step 7: Open Questions (Things to Verify)

These items cannot be determined from API schemas alone. Recommend opening a support case:

### SP
- How to clear campaign `endDateTime` to "no end date" (null may not work as expected)
- Whether `defaultBid` has a server-side default value when not provided
- Exact behavior of `PRODUCT_SIMILAR` match type (is it identical to v3's `ASIN_EXPANDED_FROM`?)

### SB
- Exact `goal` → `goalSettings.kpi` mapping (PAGE_VISIT→CLICKS, BRAND_IMPRESSION_SHARE→TOP_OF_SEARCH_IMPRESSION_SHARE)
- Whether `audienceId` values from v4 are directly portable
- How "default audience" is expressed without `audienceSegmentType`

### Both
- `costType` not returned in SP campaign response — is this by design?
- `status` field not present on all objects — when is it populated?
- Timeline for v3/v4 sunset (when will legacy APIs stop working?)
