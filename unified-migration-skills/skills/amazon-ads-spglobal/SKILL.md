---
name: amazon-ads-spglobal
description: "Use when a developer needs to integrate Amazon Ads SP Global Campaigns — managing Sponsored Products campaigns across multiple marketplaces simultaneously via the Unified API (/adsApi/v1/). Covers global campaign creation, per-marketplace overrides, multi-marketplace budgets/bids, product mapping, and differences from single-marketplace SP campaigns."
---

# Amazon Ads SP Global Campaigns — Integration Guide

## 1. What is SP Global Campaigns?

SP Global (Sponsored Products Global) is a product designed to simplify managing Sponsored Products advertising across multiple Amazon marketplaces from a single campaign. Instead of creating separate campaigns per country, advertisers create one **global campaign** that spans multiple marketplaces (US, UK, DE, FR, JP, etc.) with per-marketplace overrides for budget, bid, state, and product ASINs.

### Target Users

- Sellers/vendors advertising in 2+ countries looking for efficiency
- Sellers/vendors expanding into new countries
- API integrators building multi-marketplace campaign management tools

### Key Value Propositions

- **One campaign → multiple marketplaces**: Create once, manage everywhere
- **Per-marketplace overrides**: Customize budget, bid, state, name per country without separate campaigns
- **Auto-scale**: Optionally auto-expand to new marketplaces as they become available
- **Unified management**: Single API call to query/update across all marketplaces

## 2. Relationship to SP Unified API

SPG uses the **same 19 endpoints** as SP Unified API:

```
/adsApi/v1/{create|query|update|delete}/{campaigns|adGroups|ads|targets|adExtensions}
```

The difference is entirely in the **schema structure** — SPG schemas are prefixed `SPGlobal*` and add marketplace-aware fields to every resource.

### Architectural Comparison

| Aspect | SP (Single Marketplace) | SPG (Global) |
|--------|------------------------|--------------|
| Design | 1 campaign = 1 marketplace | 1 campaign = N marketplaces |
| Schema prefix | `SP*` | `SPGlobal*` |
| `marketplaceScope` | Optional | **Required** (always `GLOBAL`) |
| `marketplaces[]` | Optional | **Required** on all creates |
| Budget | Single value | Per-marketplace `marketplaceSettings[]` |
| Bid | Single `defaultBid` | Per-marketplace `marketplaceSettings[]` |
| Product ASIN | Single `productId` | Per-marketplace `marketplaceSettings[{marketplace, productId}]` |
| State override | N/A | `marketplaceConfigurations[].overrides.state` |
| AccountId header | Optional | **Required** |
| Scope header | Supported | **Not used** |

## 3. Headers

```bash
# Required
-H "Amazon-Ads-AccountId: ${ACCOUNT_ID}"     # REQUIRED (not optional like SP)
-H "Amazon-Ads-ClientId: ${CLIENT_ID}"
-H "Authorization: Bearer ${ACCESS_TOKEN}"
-H "Content-Type: application/json"

# NOT used in SPG (unlike SP):
# -H "Amazon-Advertising-API-Scope: ${PROFILE_ID}"
```

## 4. SPG-Specific Concepts

### 4.1 marketplaceScope (Required on all creates)

Always `"GLOBAL"`. Marks the resource as a global (multi-marketplace) resource.

### 4.2 marketplaces[] (Required on all creates)

Array of country codes where this resource applies. Must be a subset of parent resource's marketplaces.

```json
"marketplaces": ["US", "GB", "DE", "FR", "IT", "ES"]
```

Supported marketplaces: `AE`, `AU`, `BE`, `BR`, `CA`, `DE`, `EG`, `ES`, `FR`, `GB`, `IN`, `IT`, `JP`, `MX`, `NL`, `PL`, `SA`, `SE`, `SG`, `TR`, `US`

### 4.3 marketplaceConfigurations[] (Per-marketplace overrides)

Every resource (campaign, adGroup, ad, target) supports per-marketplace overrides. When not specified, the global value applies.

```json
"marketplaceConfigurations": [
  {
    "marketplace": "DE",
    "overrides": {
      "state": "PAUSED",
      "name": "SP Campaign - Germany (Paused)"
    }
  }
]
```

**Campaign overridable fields**: `state`, `name`, `startDateTime`, `endDateTime`, `optimizations`, `tags`
**AdGroup overridable fields**: `state`, `name`, `tags`
**Ad overridable fields**: `state`, `tags`
**Target overridable fields**: `state`, `tags`, `targetDetails` (keyword/theme only)

### 4.4 autoScaleGlobalCampaign

Controls whether the campaign auto-expands to new marketplaces.

| Value | Behavior |
|-------|----------|
| `AUTO` | Auto-add new marketplaces as they become available |
| `MANUAL` | Only expand when explicitly updated |

### 4.5 targetLevel

SPG targets can be attached at two levels:

| Value | Meaning |
|-------|---------|
| `AD_GROUP` | Target belongs to an ad group (standard) |
| `CAMPAIGN` | Target belongs to the campaign directly (negative targets) |

### 4.6 siteRestrictions

Restrict ad delivery to specific Amazon properties:

| Value | Description |
|-------|-------------|
| `AMAZON_BUSINESS` | Only show on Amazon Business |
| `AMAZON_HAUL` | Only show on Amazon Haul |

### 4.7 BidStrategy (includes NEW_TO_BRAND)

| Strategy | Description |
|----------|-------------|
| `MANUAL` | Uses exact bid, no dynamic adjustments |
| `SALES_DOWN_ONLY` | Decreases bids when less likely to convert |
| `SALES_UP_AND_DOWN` | Increases/decreases bids by up to 100% |
| `RULE_BASED` | Applies advertiser-defined bidding rules |
| `NEW_TO_BRAND` | **SPG-only** — Optimizes for new-to-brand customer acquisitions |

### 4.8 nativeLanguageKeyword

Keywords support native language with locale:

```json
{
  "keyword": "手机壳",
  "matchType": "BROAD",
  "nativeLanguageKeyword": "手机壳",
  "nativeLanguageLocale": "zh_CN"
}
```

## 5. Campaign Create

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `adProduct` | `"SPONSORED_PRODUCTS"` | Always SPONSORED_PRODUCTS |
| `marketplaceScope` | `"GLOBAL"` | Always GLOBAL |
| `name` | string | Campaign name |
| `state` | `ENABLED` or `PAUSED` | Initial state |
| `startDateTime` | datetime | Start time |
| `autoCreationSettings` | object | `{autoCreateTargets: boolean}` |
| `budgets` | array | Per-marketplace budget settings |

### Budget Structure (Per-Marketplace)

```json
"budgets": [{
  "budgetType": "MONETARY",
  "budgetValue": {
    "monetaryBudgetValue": {
      "marketplaceSettings": [
        {"marketplace": "US", "monetaryBudget": {"value": 50.00}},
        {"marketplace": "GB", "monetaryBudget": {"value": 40.00}},
        {"marketplace": "DE", "monetaryBudget": {"value": 45.00}}
      ]
    }
  },
  "recurrenceTimePeriod": "DAILY"
}]
```

### Complete Campaign Create Example

```json
POST /adsApi/v1/create/campaigns

{
  "campaigns": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "marketplaceScope": "GLOBAL",
    "marketplaces": ["US", "GB", "DE"],
    "name": "SP Global - Electronics Q4",
    "state": "ENABLED",
    "startDateTime": "2026-10-01T00:00:00Z",
    "autoCreationSettings": {"autoCreateTargets": true},
    "budgets": [{
      "budgetType": "MONETARY",
      "budgetValue": {
        "monetaryBudgetValue": {
          "marketplaceSettings": [
            {"marketplace": "US", "monetaryBudget": {"value": 100.00}},
            {"marketplace": "GB", "monetaryBudget": {"value": 80.00}},
            {"marketplace": "DE", "monetaryBudget": {"value": 90.00}}
          ]
        }
      },
      "recurrenceTimePeriod": "DAILY"
    }],
    "optimizations": {
      "bidSettings": {
        "bidStrategy": "SALES_UP_AND_DOWN",
        "bidAdjustments": {
          "placementBidAdjustments": [
            {"placement": "TOP_OF_SEARCH", "percentage": 50}
          ]
        }
      }
    },
    "marketplaceConfigurations": [
      {
        "marketplace": "DE",
        "overrides": {
          "name": "SP Global - Elektronik Q4",
          "startDateTime": "2026-10-02T00:00:00Z"
        }
      }
    ]
  }]
}
```

## 6. AdGroup Create

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `adProduct` | `"SPONSORED_PRODUCTS"` | Always |
| `marketplaceScope` | `"GLOBAL"` | Always |
| `marketplaces` | array | Subset of campaign's marketplaces |
| `campaignId` | string | Parent campaign |
| `name` | string | Ad group name |
| `state` | `ENABLED`/`PAUSED` | Initial state |
| `bid` | object | Per-marketplace bid settings |

### Bid Structure (Per-Marketplace)

```json
"bid": {
  "marketplaceSettings": [
    {"marketplace": "US", "currencyCode": "USD", "defaultBid": 1.50},
    {"marketplace": "GB", "currencyCode": "GBP", "defaultBid": 1.20},
    {"marketplace": "DE", "currencyCode": "EUR", "defaultBid": 1.30}
  ]
}
```

### Complete AdGroup Create Example

```json
POST /adsApi/v1/create/adGroups

{
  "adGroups": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "marketplaceScope": "GLOBAL",
    "marketplaces": ["US", "GB", "DE"],
    "campaignId": "CAMPAIGN_ID",
    "name": "Phone Cases - All Markets",
    "state": "ENABLED",
    "bid": {
      "marketplaceSettings": [
        {"marketplace": "US", "currencyCode": "USD", "defaultBid": 1.50},
        {"marketplace": "GB", "currencyCode": "GBP", "defaultBid": 1.20},
        {"marketplace": "DE", "currencyCode": "EUR", "defaultBid": 1.30}
      ]
    }
  }]
}
```

## 7. Ad Create (Product Ad with Per-Marketplace ASIN)

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `adProduct` | `"SPONSORED_PRODUCTS"` | Always |
| `adType` | `"PRODUCT_AD"` | Always PRODUCT_AD |
| `marketplaceScope` | `"GLOBAL"` | Always |
| `marketplaces` | array | Subset of adGroup's marketplaces |
| `adGroupId` | string | Parent ad group |
| `state` | `ENABLED`/`PAUSED` | Initial state |
| `creative` | object | Per-marketplace product settings |

### Per-Marketplace Product Creative

```json
POST /adsApi/v1/create/ads

{
  "ads": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "adType": "PRODUCT_AD",
    "marketplaceScope": "GLOBAL",
    "marketplaces": ["US", "GB", "DE"],
    "adGroupId": "AD_GROUP_ID",
    "state": "ENABLED",
    "creative": {
      "productCreative": {
        "productCreativeSettings": {
          "advertisedProduct": {
            "productIdType": "ASIN",
            "marketplaceSettings": [
              {"marketplace": "US", "productId": "B0ASIN_US"},
              {"marketplace": "GB", "productId": "B0ASIN_UK"},
              {"marketplace": "DE", "productId": "B0ASIN_DE"}
            ]
          }
        }
      }
    }
  }]
}
```

**Note**: `globalStoreSetting.catalogSourceMarketplace` can be used to specify where the product catalog is sourced from for a particular marketplace.

## 8. Target Create

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `adProduct` | `"SPONSORED_PRODUCTS"` | Always |
| `marketplaceScope` | `"GLOBAL"` | Always |
| `marketplaces` | array | Where this target applies |
| `negative` | boolean | Is it a negative target? |
| `state` | `ENABLED`/`PAUSED` | Initial state |
| `targetType` | enum | `KEYWORD`, `PRODUCT`, `PRODUCT_CATEGORY`, `THEME` |
| `targetDetails` | object | Target-type-specific details |

### Target Types

| targetType | targetDetails key | Description |
|------------|------------------|-------------|
| `KEYWORD` | `keywordTarget` | Customer search term targeting |
| `PRODUCT` | `productTarget` | Specific ASIN targeting |
| `PRODUCT_CATEGORY` | `productCategoryTarget` | Category targeting with refinements |
| `THEME` | `themeTarget` | Auto-targets (close match, loose match, complements, substitutes) |

### Keyword Target Example

```json
POST /adsApi/v1/create/targets

{
  "targets": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "marketplaceScope": "GLOBAL",
    "marketplaces": ["US", "GB", "DE"],
    "adGroupId": "AD_GROUP_ID",
    "negative": false,
    "state": "ENABLED",
    "targetType": "KEYWORD",
    "targetDetails": {
      "keywordTarget": {
        "keyword": "phone case",
        "matchType": "BROAD"
      }
    },
    "bid": {
      "marketplaceSettings": [
        {"marketplace": "US", "currencyCode": "USD", "bid": 2.00},
        {"marketplace": "GB", "currencyCode": "GBP", "bid": 1.60},
        {"marketplace": "DE", "currencyCode": "EUR", "bid": 1.80}
      ]
    },
    "marketplaceConfigurations": [
      {
        "marketplace": "DE",
        "overrides": {
          "targetDetails": {
            "keywordTarget": {
              "keyword": "Handyhülle",
              "matchType": "BROAD"
            }
          }
        }
      }
    ]
  }]
}
```

### Product Target Example (Per-Marketplace ASIN)

```json
{
  "targets": [{
    "adProduct": "SPONSORED_PRODUCTS",
    "marketplaceScope": "GLOBAL",
    "marketplaces": ["US", "GB"],
    "adGroupId": "AD_GROUP_ID",
    "negative": false,
    "state": "ENABLED",
    "targetType": "PRODUCT",
    "targetDetails": {
      "productTarget": {
        "matchType": "PRODUCT_EXACT",
        "productIdType": "ASIN",
        "product": {
          "marketplaceSettings": [
            {"marketplace": "US", "productId": "B0COMPETITOR_US"},
            {"marketplace": "GB", "productId": "B0COMPETITOR_UK"}
          ]
        }
      }
    }
  }]
}
```

## 9. SP Features NOT Available in SPG

| Feature | SP | SPG | Notes |
|---------|-----|-----|-------|
| Video / Spotlight creative | ✅ | ❌ | SPG is product ads only |
| Audience bid adjustment | ✅ | ❌ | |
| Creative bid adjustment (Spotlight) | ✅ | ❌ | |
| Location targeting | ✅ | ❌ | |
| Off-Amazon budget control strategy | ✅ | ❌ | |
| Marketplace budget allocation (AUTO/MANUAL) | ✅ | ❌ | SPG uses explicit per-marketplace budgets |
| Update creative content | ✅ | ❌ | |
| Keyword filter on query | ✅ | ❌ | |
| Marketplaces: IE, ZA | ✅ | ❌ | Not yet supported in SPG |
| NEW_TO_BRAND bid strategy | ❌ | ✅ | SPG-only |

## 10. Common Integration Patterns

### Pattern A: Expand existing campaign to a new marketplace

```json
PUT /adsApi/v1/update/campaigns

{
  "campaigns": [{
    "campaignId": "CAMPAIGN_ID",
    "marketplaces": ["US", "GB", "DE", "FR"]
  }]
}
```

Then add budget for the new marketplace and update ad group/ad/target marketplaces accordingly.

### Pattern B: Pause in one marketplace only

```json
PUT /adsApi/v1/update/campaigns

{
  "campaigns": [{
    "campaignId": "CAMPAIGN_ID",
    "marketplaceConfigurations": [
      {"marketplace": "DE", "overrides": {"state": "PAUSED"}}
    ]
  }]
}
```

### Pattern C: Different keyword per marketplace

Use `marketplaceConfigurations` on targets to override `targetDetails`:

```json
{
  "targetDetails": {"keywordTarget": {"keyword": "phone case", "matchType": "BROAD"}},
  "marketplaceConfigurations": [
    {"marketplace": "DE", "overrides": {"targetDetails": {"keywordTarget": {"keyword": "Handyhülle", "matchType": "BROAD"}}}},
    {"marketplace": "FR", "overrides": {"targetDetails": {"keywordTarget": {"keyword": "coque téléphone", "matchType": "BROAD"}}}}
  ]
}
```

## 11. FAQ

**Q: Can I mix SP and SPG campaigns under the same account?**
A: Yes. They are different campaign types managed independently.

**Q: What happens if I don't specify `marketplaceConfigurations`?**
A: The global value applies uniformly to all marketplaces.

**Q: Can I use different bid strategies per marketplace?**
A: No. `bidStrategy` is campaign-level (global). Only bid **values** can differ per marketplace.

**Q: How does `autoScaleGlobalCampaign: AUTO` work?**
A: When new marketplaces become available for the account, the campaign auto-expands to include them.

**Q: What's the `currencyCode` in bid/budget marketplace settings?**
A: Must match the marketplace's local currency (USD for US, GBP for GB, EUR for DE/FR/IT/ES, etc.).

**Q: Can I create a global campaign with just one marketplace?**
A: Yes. You can start with one marketplace and expand later.

**Q: What's `targetLevel: CAMPAIGN` used for?**
A: Campaign-level negative targets that apply across all ad groups in the campaign.
