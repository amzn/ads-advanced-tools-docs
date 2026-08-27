---
name: unified-dsp-cm-migration
description: "Use when migrating Amazon DSP Campaign Management from legacy APIs (/dsp/v1/) to the Unified API (/adsApi/v1/), including campaign, ad group, creative/ad, target, and ad association endpoint mapping, request body restructuring, header changes, and field-level differences."
---

# Amazon DSP: Legacy API to Unified API — Campaign Management Migration Guide

## 1. Overview & Migration Scope

This guide helps developers migrate from the **legacy DSP Campaign Management APIs** (`/dsp/v1/...`) to the **Unified API** (`/adsApi/v1/...`).

### Who Should Use This

Developers currently using:
- `/dsp/v1/campaigns` — Campaign CRUD
- `/dsp/v1/adGroups` — Ad Group CRUD
- `/dsp/v1/targets` — Target management
- `/dsp/v1/adCreatives` — Creative management
- `/dsp/v1/adCreatives/associations/adGroups` — Creative-to-AdGroup linking
- `/dsp/v1/campaigns/{id}/flights` — Flight management

### What's Covered (Migrating to Unified API)

| Resource | Legacy API | Unified API |
|----------|-----------|-------------|
| Campaigns | `/dsp/v1/campaigns` | `/adsApi/v1/create/campaigns` |
| Ad Groups | `/dsp/v1/adGroups` | `/adsApi/v1/create/adGroups` |
| Ads (Creatives) | `/dsp/v1/adCreatives` | `/adsApi/v1/create/ads` |
| Ad Associations | `/dsp/v1/adCreatives/associations/adGroups` | `/adsApi/v1/create/adAssociations` |
| Targets | `/dsp/v1/targets` | `/adsApi/v1/create/targets` |
| Flights | `/dsp/v1/campaigns/{id}/flights` | Nested in campaign create/update |

### What's NOT Covered (Remain on Legacy)

These legacy APIs have no equivalent in the Unified API yet:
- Measurement & Studies (30 endpoints)
- Deal Proposals (19 endpoints)
- Deals management (8 endpoints)
- Deal Advertiser Permissions (8 endpoints)
- Conversion Tracking (20 endpoints)
- Bid Adjustment Rules (8 endpoints)
- Frequency Groups (10 endpoints)
- Inherited Settings (6 endpoints)
- Guidance (3 endpoints)
- QuickActions (5 endpoints)
- Seats (4 endpoints)
- Campaign Insights (3 endpoints)
- Discovery APIs (8 endpoints) — partially replaced by query filters
- Audiences (3 endpoints)

---

## 2. Architecture Changes

| Aspect | Legacy DSP | Unified DSP |
|--------|-----------|-------------|
| Endpoint pattern | `/dsp/v1/{resource}` | `/adsApi/v1/{action}/{resource}` |
| HTTP methods | Mixed: POST (create), PATCH (update), POST (list) | **All POST** |
| Header: Client ID | `Amazon-Advertising-API-ClientId` | `Amazon-Ads-ClientId` |
| Header: Scope | `Amazon-Advertising-API-Scope` (required) | **Not used** |
| Header: Account ID | `Amazon-Ads-AccountId` (required) | `Amazon-Ads-AccountId` (required) |
| `adProduct` field | Not required (implicit) | **Required**: `"AMAZON_DSP"` |
| State values | `ACTIVE` / `INACTIVE` | `ENABLED` / `PAUSED` / `ARCHIVED` |
| Campaign creation state | Can create as `ACTIVE` | **Must create as `PAUSED`**, then update to `ENABLED` |
| Response structure | `{requestId, success:[], error:[]}` | `{success:[], error:[]}` (no requestId at top level) |
| Batch size (campaigns) | 5 | 5 |
| Batch size (ad groups) | 20 | 20 |
| Batch size (ads/creatives) | varies | 10 |
| Batch size (targets) | 1000 | 1000 |

### Header Comparison

```bash
# Legacy DSP
-H "Amazon-Advertising-API-ClientId: ${CLIENT_ID}"
-H "Amazon-Ads-AccountId: ${ACCOUNT_ID}"
-H "Amazon-Advertising-API-Scope: ${PROFILE_ID}"
-H "Content-Type: application/json"

# Unified DSP
-H "Amazon-Ads-ClientId: ${CLIENT_ID}"
-H "Amazon-Ads-AccountId: ${ACCOUNT_ID}"
-H "Content-Type: application/json"
# No Scope header needed
```

---

## 3. Endpoint Mapping Table

| Legacy Endpoint | Method | Unified Endpoint | Method |
|----------------|--------|-----------------|--------|
| `/dsp/v1/campaigns` | POST | `/adsApi/v1/create/campaigns` | POST |
| `/dsp/v1/campaigns` | PATCH | `/adsApi/v1/update/campaigns` | POST |
| `/dsp/v1/campaigns/list` | POST | `/adsApi/v1/query/campaigns` | POST |
| `/dsp/v1/adGroups` | POST | `/adsApi/v1/create/adGroups` | POST |
| `/dsp/v1/adGroups` | PATCH | `/adsApi/v1/update/adGroups` | POST |
| `/dsp/v1/adGroups/list` | POST | `/adsApi/v1/query/adGroups` | POST |
| `/dsp/v1/targets` | POST | `/adsApi/v1/create/targets` | POST |
| `/dsp/v1/targets/list` | POST | `/adsApi/v1/query/targets` | POST |
| `/dsp/v1/targets/delete` | POST | `/adsApi/v1/delete/targets` | POST |
| `/dsp/v1/targets/deleteByObject` | POST | _(deprecated, use delete by ID)_ | — |
| `/dsp/v1/adCreatives` | POST | `/adsApi/v1/create/ads` | POST |
| `/dsp/v1/adCreatives/list` | POST | `/adsApi/v1/query/ads` | POST |
| `/dsp/v1/adCreatives/{id}` | PATCH | `/adsApi/v1/update/ads` | POST |
| `/dsp/v1/adCreatives/associations/adGroups` | POST | `/adsApi/v1/create/adAssociations` | POST |
| `/dsp/v1/adCreatives/associations/adGroups` | PATCH | `/adsApi/v1/update/adAssociations` | POST |
| `/dsp/v1/adCreatives/associations/adGroups/list` | POST | `/adsApi/v1/query/adAssociations` | POST |
| `/dsp/v1/adCreatives/associations/adGroups/delete` | POST | `/adsApi/v1/delete/adAssociations` | POST |
| `/dsp/v1/campaigns/{id}/flights` | POST/PATCH | Nested in campaign `flights` field | — |

---

## 4. Campaign Migration

### Required Fields Comparison

| Legacy (`DspPostCampaign`) | Unified (`DSPCampaignCreate`) |
|---------------------------|------------------------------|
| `advertiserId` | Not needed (derived from AccountId header) |
| `flights` (required) | `flights` (required) |
| `frequencies` (required) | `frequencies` (optional) |
| `name` (required) | `name` (required) |
| `optimization` (required) | `optimizations` (required) |
| — | `adProduct`: `"AMAZON_DSP"` (required) |
| — | `state`: `"PAUSED"` (required on create) |

### Key Field Mapping

| Legacy Field | Unified Field | Notes |
|-------------|--------------|-------|
| `advertiserId` | _(removed)_ | Derived from `Amazon-Ads-AccountId` header |
| `optimization.goalSetting` | `optimizations.goalSettings` | Renamed (pluralized) |
| `optimization.bidStrategy` | `optimizations.bidSettings.bidStrategy` | Nested deeper |
| `optimization.automateBudgetAllocation` | `optimizations.budgetSettings.budgetAllocation` | Restructured |
| `optimization.flightBudgetRolloverSetting` | `optimizations.budgetSettings.flightBudgetRolloverStrategy` | Renamed |
| `state`: `ACTIVE`/`INACTIVE` | `state`: `ENABLED`/`PAUSED`/`ARCHIVED` | Enum changed |
| `country` | `countries[]` | String → array |
| `budgetCaps[]` | `budgets[]` | Restructured with `budgetType`/`budgetValue`/`recurrenceTimePeriod` |
| `fees` | `fees` | Similar structure |
| `flights[]` | `flights[]` | Structure similar, see below |

### Flight Structure Comparison

```json
// Legacy flight
{
  "budgetAmount": 10000.00,
  "startDateTime": "2026-01-01T00:00:00Z",
  "endDateTime": "2026-01-31T23:59:59Z",
  "name": "January Flight"
}

// Unified flight
{
  "budget": {
    "budgetType": "MONETARY",
    "budgetValue": {
      "monetaryBudgetValue": {
        "monetaryBudget": { "value": 10000.00 }
      }
    }
  },
  "startDateTime": "2026-01-01T00:00:00Z",
  "endDateTime": "2026-01-31T23:59:59Z"
}
```

### Before/After Example

```json
// LEGACY: POST /dsp/v1/campaigns
{
  "campaigns": [{
    "advertiserId": "ADV123",
    "name": "Q1 Display Campaign",
    "state": "ACTIVE",
    "country": "US",
    "flights": [{
      "budgetAmount": 50000,
      "startDateTime": "2026-01-01T00:00:00Z",
      "endDateTime": "2026-03-31T23:59:59Z"
    }],
    "frequencies": [{"frequencyType": "CUSTOM", "maxImpressions": 5, "timeUnit": "DAYS", "timeUnitCount": 1}],
    "optimization": {
      "bidStrategy": "SPEND_BUDGET_IN_FULL",
      "goalSetting": {"kpi": "CLICK_THROUGH_RATE"}
    }
  }]
}

// UNIFIED: POST /adsApi/v1/create/campaigns
{
  "campaigns": [{
    "adProduct": "AMAZON_DSP",
    "name": "Q1 Display Campaign",
    "state": "PAUSED",
    "countries": ["US"],
    "flights": [{
      "budget": {
        "budgetType": "MONETARY",
        "budgetValue": {"monetaryBudgetValue": {"monetaryBudget": {"value": 50000}}}
      },
      "startDateTime": "2026-01-01T00:00:00Z",
      "endDateTime": "2026-03-31T23:59:59Z"
    }],
    "frequencies": [{"eventMaxCount": 5, "timeUnit": "DAYS", "timeCount": 1, "frequencyTargetingSetting": "IMPRESSION"}],
    "optimizations": {
      "bidSettings": {"bidStrategy": "SPEND_BUDGET_IN_FULL"},
      "goalSettings": {"kpi": "CLICK_THROUGH_RATE"}
    }
  }]
}
```

> ⚠️ After creating the campaign in `PAUSED` state, update it to `ENABLED` to activate delivery.

---

## 5. AdGroup Migration

### Required Fields Comparison

| Legacy (`DspPostAdGroup`) | Unified (`DSPAdGroupCreate`) |
|--------------------------|------------------------------|
| `advertisedProductCategoryIds` | `advertisedProductCategoryIds` |
| `bid` (`{baseBid}`) | `bid` (`{baseBid, currencyCode}`) |
| `campaignId` | `campaignId` |
| `creativeRotationType` | `creativeRotationType` |
| `endDateTime` | `endDateTime` |
| `frequencies` | _(optional)_ |
| `inventoryType` | `inventoryType` |
| `name` | `name` |
| `optimization` (`{bidStrategy}`) | `optimization` (`{bidStrategy}`) |
| `pacing` (`{deliveryProfile, catchUpBoostPercentage}`) | `pacing` (`{deliveryProfile}`) |
| `startDateTime` | `startDateTime` |
| `targetingSettings` | `targetingSettings` |
| — | `adProduct`: `"AMAZON_DSP"` (required) |
| — | `state`: `"PAUSED"` (required) |

### Key Changes

- **`bid`**: Legacy only requires `baseBid`. Unified requires `baseBid` (currencyCode derived from campaign in create).
- **`pacing.catchUpBoostPercentage`**: Deprecated in legacy, removed in Unified.
- **`state`**: Must be `PAUSED` on create, update to `ENABLED` to activate.
- **`targetingSettings`**: Mostly compatible. Key sub-fields:
  - `amazonViewability` — same structure
  - `timeZoneType` — same values (`ADVERTISER_REGION`, `VIEWER`)
  - `userLocationSignal` — same values
  - `automatedTargetingTactic` — renamed values in some cases
  - `videoCompletionTier` — same structure

### Before/After Example

```json
// LEGACY: POST /dsp/v1/adGroups
{
  "adGroups": [{
    "campaignId": "CAMP123",
    "name": "Display AdGroup",
    "inventoryType": "DISPLAY",
    "advertisedProductCategoryIds": ["12345"],
    "bid": {"baseBid": 3.50},
    "creativeRotationType": "RANDOM",
    "startDateTime": "2026-01-01T00:00:00Z",
    "endDateTime": "2026-03-31T23:59:59Z",
    "frequencies": [{"frequencyType": "CUSTOM", "maxImpressions": 10, "timeUnit": "DAYS", "timeUnitCount": 1}],
    "optimization": {"bidStrategy": "SPEND_BUDGET_IN_FULL"},
    "pacing": {"deliveryProfile": "EVEN", "catchUpBoostPercentage": 0},
    "targetingSettings": {
      "amazonViewability": {"viewabilityTier": "ALL_TIERS", "includeUnmeasurableImpressions": false},
      "timeZoneType": "VIEWER",
      "userLocationSignal": "ANYWHERE"
    }
  }]
}

// UNIFIED: POST /adsApi/v1/create/adGroups
{
  "adGroups": [{
    "adProduct": "AMAZON_DSP",
    "campaignId": "CAMP123",
    "name": "Display AdGroup",
    "state": "PAUSED",
    "inventoryType": "DISPLAY",
    "advertisedProductCategoryIds": ["12345"],
    "bid": {"baseBid": 3.50},
    "creativeRotationType": "RANDOM",
    "startDateTime": "2026-01-01T00:00:00Z",
    "endDateTime": "2026-03-31T23:59:59Z",
    "frequencies": [{"eventMaxCount": 10, "timeUnit": "DAYS", "timeCount": 1, "frequencyTargetingSetting": "IMPRESSION"}],
    "optimization": {"bidStrategy": "SPEND_BUDGET_IN_FULL"},
    "pacing": {"deliveryProfile": "EVEN"},
    "targetingSettings": {
      "amazonViewability": {"viewabilityTier": "ALL_TIERS", "includeUnmeasurableImpressions": false},
      "timeZoneType": "VIEWER",
      "userLocationSignal": "ANYWHERE"
    }
  }]
}
```

---

## 6. Targets Migration

### Structural Change

The key difference is how target type is expressed:

| Aspect | Legacy `/dsp/v1/targets` | Unified `/adsApi/v1/create/targets` |
|--------|-------------------------|-------------------------------------|
| Target type declaration | `targetType` enum field **inside** each target detail object | **oneOf key** in `targetDetails` (no separate `targetType` field) |
| Target type values | Uppercase enum: `AUDIENCE`, `DEVICE`, etc. | camelCase object key: `audienceTarget`, `deviceTarget`, etc. |

### Target Type Mapping

| Legacy `targetType` Enum | Unified `targetDetails` Key |
|--------------------------|----------------------------|
| `AUDIENCE` | `audienceTarget` |
| `APP` | `appTarget` |
| `DAY_PART` | `dayPartTarget` |
| `DEVICE` | `deviceTarget` |
| `LOCATION` | `locationTarget` |
| `INVENTORY_SOURCE` | `inventorySourceTarget` |
| `PRODUCT` | `productTarget` |
| `PRODUCT_CATEGORY` | `productCategoryTarget` |
| `IAB_CATEGORY` | `contentCategoryTarget` |
| `CONTENT_GENRE` | `contentGenreTarget` |
| `CONTENT_RATING` | `contentRatingTarget` |
| `AD_INITIATION` | `adInitiationTarget` |
| `AD_PLAYER_SIZE` | `adPlayerSizeTarget` |
| `VIDEO_AD_FORMAT` | `videoAdFormatTarget` |
| `THIRD_PARTY` | `thirdPartyTarget` |
| `DOMAIN` | `domainTarget` |
| `KEYWORD` | `keywordTarget` |
| `BRAND_SAFETY_TIER` | `brandSafetyTierTarget` |
| `BRAND_SAFETY_CATEGORY` | `brandSafetyCategoryTarget` |
| `CONTENT_INSTREAM_POSITION` | `contentInstreamPositionTarget` |
| `CONTENT_OUTSTREAM_POSITION` | `contentOutstreamPositionTarget` |
| `CONTENT_DURATION` | `videoContentDurationTarget` |
| `ON_SCREEN_POSITION` | `foldPositionTarget` |
| `NATIVE_CONTENT_POSITION` | `nativeContentPositionTarget` |
| `PLACEMENT_TYPE` | `placementTypeTarget` |
| `AUTO` | `themeTarget` |
| `SAVED_GROUP` | _(not available in Unified)_ |

### Before/After Example (Audience Target)

```json
// LEGACY: POST /dsp/v1/targets
{
  "targets": [{
    "adGroupId": "AG123",
    "adProduct": "AMAZON_DSP",
    "negative": false,
    "state": "ENABLED",
    "targetDetails": {
      "audienceTarget": {
        "audienceId": "AUD456",
        "groupId": "1",
        "targetType": "AUDIENCE"
      }
    }
  }]
}

// UNIFIED: POST /adsApi/v1/create/targets
{
  "targets": [{
    "adGroupId": "AG123",
    "adProduct": "AMAZON_DSP",
    "negative": false,
    "state": "ENABLED",
    "targetDetails": {
      "audienceTarget": {
        "audienceId": {"defaultValue": "AUD456"},
        "groupId": "1"
      }
    }
  }]
}
```

**Key differences:**
- No `targetType` field inside the target detail object in Unified
- `audienceId` changed from plain string to `{"defaultValue": "..."}` (marketplace-aware)

---

## 7. Ads & Creative Migration

### Conceptual Change

| Aspect | Legacy | Unified |
|--------|--------|---------|
| Creative entity | `/dsp/v1/adCreatives` — standalone creative | `/adsApi/v1/create/ads` — "ad" contains creative |
| Linking to ad group | `/dsp/v1/adCreatives/associations/adGroups` | `/adsApi/v1/create/adAssociations` |
| Creative types | Nested in creative settings | `adType` enum + `creative` oneOf |

### Ad Types

| Unified `adType` | Creative `oneOf` Key | Description |
|-----------------|---------------------|-------------|
| `AUDIO` | `audioCreative` | Audio ads (streaming audio/podcast) |
| `COMPONENT` | `componentCreative` | Responsive ecommerce, asset-based, brand store |
| `DISPLAY` | `displayCreative` | Standard display with custom images |
| `THIRD_PARTY` | `thirdPartyCreative` | Third-party ad server hosted |
| `VIDEO` | `videoCreative` | Online video (OLV) and Streaming TV (STV) |

### Before/After Example (Responsive Ecommerce Ad)

```json
// LEGACY: POST /dsp/v1/adCreatives
// (creates a creative, then associate separately)
{
  "adCreatives": [{
    "adType": "COMPONENT",
    "creativeSettings": {
      "responsiveEcommerceSettings": {
        "language": "EN",
        "inventoryTypes": ["DISPLAY"],
        "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
        "optimizationGoalKpi": "CLICK_THROUGH_RATE",
        "responsiveSizingBehavior": "ENABLED",
        "supportedThirdPartySellers": "DISABLED"
      }
    }
  }]
}

// UNIFIED: POST /adsApi/v1/create/ads
{
  "ads": [{
    "adProduct": "AMAZON_DSP",
    "adType": "COMPONENT",
    "name": "Responsive Ecommerce Ad",
    "state": "PAUSED",
    "creative": {
      "componentCreative": {
        "responsiveEcommerceSettings": {
          "language": "EN",
          "inventoryTypes": ["DISPLAY"],
          "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
          "optimizationGoalKpi": "CLICK_THROUGH_RATE",
          "responsiveSizingBehavior": "ENABLED",
          "supportedThirdPartySellers": "DISABLED"
        }
      }
    }
  }]
}
```

### Ad Association (Linking Ad to AdGroup)

```json
// LEGACY: POST /dsp/v1/adCreatives/associations/adGroups
{
  "associations": [{
    "adCreativeId": "CREATIVE123",
    "adGroupId": "AG123",
    "state": "ACTIVE"
  }]
}

// UNIFIED: POST /adsApi/v1/create/adAssociations
{
  "adAssociations": [{
    "adId": "AD123",
    "adGroupId": "AG123",
    "state": "ENABLED"
  }]
}
```

---

## 8. New Features in Unified API (Not Available in Legacy)

### Commitments

Manage advertising commitments/deals:
- `POST /adsApi/v1/create/commitments/dsp`
- `POST /adsApi/v1/query/commitments/dsp`
- `POST /adsApi/v1/update/commitments/dsp`
- `POST /adsApi/v1/retrieve/commitments/dsp`
- `POST /adsApi/v1/retrieve/commitmentSpends/dsp`
- `GET /adsApi/v1/commitments/dsp` (list)

### GeoLocations

Create geo-targeting definitions (smart locations + radius):
- `POST /adsApi/v1/create/geoLocations`

Supports:
- **Smart locations**: Target postal codes based on a sales index
- **Radius locations** (beta): Target users within a distance of a coordinate

### Location Indexes

Create and manage custom postal-code-to-value datasets:
- `POST /adsApi/v1/create/locationIndexes`
- `POST /adsApi/v1/update/locationIndexes`
- `POST /adsApi/v1/retrieve/locationIndexes`
- `GET /adsApi/v1/locationIndexes` (list)

### Campaign Forecasts

Retrieve delivery forecasts for campaigns:
- `POST /adsApi/v1/retrieve/campaignForecasts/dsp`

Returns: delivery confidence, expected reach/impressions, spend projections, replanning recommendations.

---

## 9. Legacy APIs NOT Migrated

These GA legacy APIs continue to function and have **no equivalent** in the Unified API. Continue using them directly:

| Category | Legacy Spec | Endpoints | Path Pattern |
|----------|-------------|-----------|--------------|
| Measurement | `Measurement_prod_3p.json` | 30 | `/dsp/measurement/...` |
| Deal Proposals | `DealProposals_prod_3p.json` | 19 | `/dsp/inventory/proposals/...` |
| Deals | `Deals_prod_3p.json` | 8 | `/dsp/inventory/deals/...` |
| Deal Permissions | `DealAdvertiserPermissions_prod_3p.json` | 8 | `/dsp/inventory/deals/{id}/advertiserPermissions/...` |
| Deal Create/Update | `D16GDspApiDeals_prod_3p.json` | 2 | `/dsp/inventory/deals` |
| Conversions | `ConversionsAPI_prod_3p.json` | 17 | `/accounts/{id}/dsp/...` |
| Conv. Tracking Products | `CampaignConversionTracking-Products-V1_prod_3p.json` | 3 | `/dsp/v1/campaigns/{id}/conversionTracking/...` |
| Bid Adjustments | `BidAdjustments_prod_3p.json` | 8 | `/dsp/rules/adjustments/...` |
| Frequency Groups | `D16GFMApiFrequencyGroup*.json` | 10 | `/frequencyGroups/v1/...` |
| Inherited Settings | `InheritedSettingsAPI-V1_prod_3p.json` | 6 | `/dsp/v1/inheritedSettings/...` |
| Guidance | `DSPGuidance_prod_3p.json` | 3 | `/dsp/v1/guidance/...` |
| QuickActions | `DSPQuickActions_prod_3p.json` | 5 | `/dsp/v1/quickactions/...` |
| Seats | `D16GSeatsApiService_prod_3p.json`, `SeatIDs_prod_3p.json` | 4 | `/dsp/v1/seats/...` |
| Insights | `D16GDspApiCampaignInsightsV1_prod_3p.json`, `D16GDspApiActionableInsights_prod_3p.json` | 3 | `/dsp/v1/campaign/insights`, `/dsp/v1/frequency*Insights/...` |
| Discovery | 7 files | 8 | `/dsp/v1/*/list`, `/dsp/v2/*/list`, `/dsp/geoLocations` |
| Audiences | `ADSPAudiences_prod_3p.json` | 1 | `/dsp/audiences` |
| Combined Audiences | `CombinedAudienceAPI_prod_3p.json` | 2 | `/dsp/audiences/combinedAudiences/...` |
| KPI Recommendations | `GoalSeekingBidderTargetKPIRecommendation_prod_3p.json` | 1 | `/dsp/campaigns/targetKpi/recommendations` |

---

## 10. FAQ

**Q: Can I use both legacy and Unified APIs simultaneously?**
A: Yes. You can gradually migrate resource by resource. Campaigns created via legacy are visible in the Unified query endpoints, and vice versa.

**Q: Do I need to migrate everything at once?**
A: No. The recommended approach is:
1. Start with queries (read-only, low risk)
2. Migrate campaign creation
3. Migrate ad group creation
4. Migrate targets
5. Migrate creatives/ads + associations

**Q: Why must campaigns be created in PAUSED state?**
A: This is an ADSP-specific requirement in the Unified API. Create the campaign as `PAUSED`, configure all ad groups, ads, and targets, then update the campaign state to `ENABLED` to begin delivery.

**Q: What happened to `advertiserId` in campaign create?**
A: It's no longer needed. The advertiser is determined from the `Amazon-Ads-AccountId` header.

**Q: What about the `Amazon-Advertising-API-Scope` header?**
A: Not used in the Unified API. Remove it from your requests.

**Q: How do I handle the `targetType` field change in targets?**
A: In legacy, each target detail object has a `targetType` enum field (e.g., `"targetType": "AUDIENCE"`). In Unified, the target type is determined by which key you use in `targetDetails` (e.g., `"audienceTarget": {...}`). Remove the `targetType` field from your target detail objects.

**Q: Are batch size limits the same?**
A: Mostly yes. Campaigns: 5, Ad Groups: 20, Targets: 1000. Ads (creatives) changed from variable to fixed at 10. Ad Associations: 20.

**Q: What about legacy `/dsp/v1/targets/deleteByObject`?**
A: This endpoint is deprecated even in legacy. Use delete-by-ID pattern: collect target IDs via query, then call `/adsApi/v1/delete/targets` with the IDs.
