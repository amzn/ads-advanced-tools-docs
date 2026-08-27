---
name: unified-api-cli-testing
description: "Use when a developer needs to generate curl commands, CLI test scripts, or HTTP request examples for testing Amazon Ads Unified API endpoints (/adsApi/v1/), including creating test payloads from OpenAPI specs, building auth headers, and validating API responses."
---

# Amazon Ads Unified API — CLI Testing & Code Generation Guide

## 1. Overview

This skill helps developers generate ready-to-use `curl` commands and test scripts for the Amazon Ads Unified API. It uses the OpenAPI specifications (`api-specs/unified-api-sp.json`, `api-specs/unified-api-sb.json`, `api-specs/unified-api-spglobal.json`, and `api-specs/unified-api-dsp.json`) as the source of truth for:

- Available endpoints and HTTP methods
- Required/optional request fields
- Valid enum values
- Request body schemas
- Expected response formats

### When to Use This Skill

- "Generate a curl command to create a SP campaign"
- "Generate a curl command to create a SP Global campaign across US, UK, and DE"
- "Generate a curl command to create a DSP campaign"
- "How do I test the /adsApi/v1/query/targets endpoint?"
- "Give me a script to test all CRUD operations for ad groups"
- "What headers do I need for Unified API requests?"
- "Generate test payloads for SB collection ads"
- "Generate a curl to create a global ad with different ASINs per marketplace"

---

## 2. Authentication & Headers

All Unified API requests require these headers:

```bash
# Required headers
-H "Authorization: Bearer ${ACCESS_TOKEN}" \
-H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
-H "Content-Type: application/json"

# Optional headers (but recommended)
-H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
-H "Amazon-Advertising-API-Scope: ${PROFILE_ID}"
```

### SP Global Campaign Headers

For SP Global campaigns, headers differ from SP/SB:

```bash
# SPG Required headers — AccountId is REQUIRED, Scope is NOT used
-H "Authorization: Bearer ${ACCESS_TOKEN}" \
-H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
-H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
-H "Content-Type: application/json"
# Do NOT include Amazon-Advertising-API-Scope for SPG
```

| Header | SP / SB | SP Global |
|--------|---------|-----------|
| `Amazon-Ads-AccountId` | Optional | **Required** |
| `Amazon-Advertising-API-Scope` | Optional | **Not used** |

### Environment Variable Setup

```bash
# Save these in a .env file (DO NOT commit to git)
export ADS_API_BASE="https://advertising-api.amazon.com"  # NA region
export ACCESS_TOKEN="Atza|..."
export CLIENT_ID="amzn1.application-oa2-client.xxx"
export ACCOUNT_ID="ENTITYXXXXXXXXX"
export PROFILE_ID="1234567890"
```

### Regional Endpoints

| Region | Base URL |
|--------|----------|
| North America | `https://advertising-api.amazon.com` |
| Europe | `https://advertising-api-eu.amazon.com` |
| Far East | `https://advertising-api-fe.amazon.com` |

---

## 3. Curl Command Templates

### 3.1 Campaign Operations

#### Create Campaign (SP)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [
      {
        "adProduct": "SPONSORED_PRODUCTS",
        "name": "TEST-SP-Campaign-'"$(date +%s)"'",
        "state": "PAUSED",
        "costType": "CPC",
        "marketplaceScope": "SINGLE_MARKETPLACE",
        "startDateTime": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
        "budgets": [
          {
            "budgetType": "MONETARY",
            "budgetValue": {
              "monetaryBudgetValue": {
                "monetaryBudget": { "value": 10.00 }
              }
            },
            "recurrenceTimePeriod": "DAILY"
          }
        ],
        "optimizations": {
          "bidSettings": {
            "bidStrategy": "MANUAL"
          }
        }
      }
    ]
  }' | jq .
```

#### Create Campaign (SB)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [
      {
        "adProduct": "SPONSORED_BRANDS",
        "name": "TEST-SB-Campaign-'"$(date +%s)"'",
        "state": "PAUSED",
        "costType": "CPC",
        "marketplaceScope": "SINGLE_MARKETPLACE",
        "startDateTime": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
        "brandId": "'"${BRAND_ID}"'",
        "budgets": [
          {
            "budgetType": "MONETARY",
            "budgetValue": {
              "monetaryBudgetValue": {
                "monetaryBudget": { "value": 25.00 }
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
            "bidStrategy": "MANUAL"
          }
        }
      }
    ]
  }' | jq .
```

#### Create Campaign (SP Global — Multi-Marketplace)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [
      {
        "adProduct": "SPONSORED_PRODUCTS",
        "name": "TEST-SPG-Global-Campaign-'"$(date +%s)"'",
        "state": "PAUSED",
        "marketplaceScope": "GLOBAL",
        "marketplaces": ["US", "GB", "DE"],
        "startDateTime": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
        "autoCreationSettings": { "autoCreateTargets": true },
        "budgets": [
          {
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
          }
        ],
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
              "name": "TEST-SPG-Elektronik-'"$(date +%s)"'",
              "state": "PAUSED"
            }
          }
        ]
      }
    ]
  }' | jq .
```

#### Query Campaigns (SP Global — filter by GLOBAL scope)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["SPONSORED_PRODUCTS"]
    },
    "marketplaceScopeFilter": {
      "include": ["GLOBAL"]
    },
    "stateFilter": {
      "include": ["ENABLED", "PAUSED"]
    },
    "maxResults": 10
  }' | jq .
```

#### Create Campaign (DSP)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [
      {
        "adProduct": "AMAZON_DSP",
        "name": "TEST-DSP-Campaign-'"$(date +%s)"'",
        "state": "PAUSED",
        "countries": ["US"],
        "flights": [
          {
            "budget": {
              "budgetType": "MONETARY",
              "budgetValue": {
                "monetaryBudgetValue": {
                  "monetaryBudget": { "value": 10000.00 }
                }
              }
            },
            "startDateTime": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
            "endDateTime": "'"$(date -u -v+30d +%Y-%m-%dT%H:%M:%SZ)"'"
          }
        ],
        "frequencies": [
          {
            "eventMaxCount": 5,
            "timeUnit": "DAYS",
            "timeCount": 1,
            "frequencyTargetingSetting": "IMPRESSION"
          }
        ],
        "optimizations": {
          "bidSettings": { "bidStrategy": "SPEND_BUDGET_IN_FULL" },
          "goalSettings": { "kpi": "CLICK_THROUGH_RATE" }
        }
      }
    ]
  }' | jq .
```

> ⚠️ DSP campaigns must be created in `PAUSED` state. Update to `ENABLED` after configuring ad groups and targets.

#### Query Campaigns (DSP)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["AMAZON_DSP"]
    },
    "stateFilter": {
      "include": ["ENABLED", "PAUSED"]
    },
    "maxResults": 10
  }' | jq .
```

#### Query Campaigns

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["SPONSORED_PRODUCTS"]
    },
    "stateFilter": {
      "include": ["ENABLED", "PAUSED"]
    },
    "maxResults": 10
  }' | jq .
```

#### Update Campaign

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/update/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [
      {
        "campaignId": "'"${CAMPAIGN_ID}"'",
        "state": "PAUSED"
      }
    ]
  }' | jq .
```

#### Delete Campaign

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/delete/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaignIds": ["'"${CAMPAIGN_ID}"'"]
  }' | jq .
```


### 3.2 Ad Group Operations

#### Create Ad Group

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/adGroups" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adGroups": [
      {
        "adProduct": "SPONSORED_PRODUCTS",
        "campaignId": "'"${CAMPAIGN_ID}"'",
        "name": "TEST-AdGroup-'"$(date +%s)"'",
        "state": "ENABLED",
        "bid": { "bid": 1.50 }
      }
    ]
  }' | jq .
```

#### Create Ad Group (SP Global — Per-Marketplace Bids)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/adGroups" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adGroups": [
      {
        "adProduct": "SPONSORED_PRODUCTS",
        "marketplaceScope": "GLOBAL",
        "marketplaces": ["US", "GB", "DE"],
        "campaignId": "'"${CAMPAIGN_ID}"'",
        "name": "TEST-SPG-AdGroup-'"$(date +%s)"'",
        "state": "ENABLED",
        "bid": {
          "marketplaceSettings": [
            {"marketplace": "US", "currencyCode": "USD", "defaultBid": 1.50},
            {"marketplace": "GB", "currencyCode": "GBP", "defaultBid": 1.20},
            {"marketplace": "DE", "currencyCode": "EUR", "defaultBid": 1.30}
          ]
        }
      }
    ]
  }' | jq .
```

#### Query Ad Groups

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/adGroups" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["SPONSORED_PRODUCTS"]
    },
    "campaignIdFilter": {
      "include": ["'"${CAMPAIGN_ID}"'"]
    },
    "maxResults": 50
  }' | jq .
```

### 3.3 Ad Operations

#### Create Product Ad (SP)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/ads" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "ads": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "adType": "PRODUCT_AD",
        "state": "ENABLED",
        "creative": {
          "productCreative": {
            "productCreativeSettings": {
              "advertisedProduct": {
                "productId": "'"${ASIN}"'",
                "productIdType": "ASIN"
              }
            }
          }
        }
      }
    ]
  }' | jq .
```

#### Create Product Ad (SP Global — Per-Marketplace ASINs)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/ads" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "ads": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "adType": "PRODUCT_AD",
        "marketplaceScope": "GLOBAL",
        "marketplaces": ["US", "GB", "DE"],
        "state": "ENABLED",
        "creative": {
          "productCreative": {
            "productCreativeSettings": {
              "advertisedProduct": {
                "productIdType": "ASIN",
                "marketplaceSettings": [
                  {"marketplace": "US", "productId": "B0US_ASIN_1"},
                  {"marketplace": "GB", "productId": "B0UK_ASIN_1"},
                  {"marketplace": "DE", "productId": "B0DE_ASIN_1"}
                ]
              }
            }
          }
        }
      }
    ]
  }' | jq .
```

#### Create Auto Collection Ad (SB)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/ads" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "ads": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_BRANDS",
        "adType": "COMPONENT",
        "name": "TEST-AutoCollection-'"$(date +%s)"'",
        "state": "ENABLED",
        "creative": {
          "componentCreative": {
            "autoCollectionSettings": {
              "sharedSettings": {
                "brand": "'"${BRAND_NAME}"'"
              }
            }
          }
        }
      }
    ]
  }' | jq .
```

#### Create Manual Collection Ad (SB)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/ads" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "ads": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_BRANDS",
        "adType": "COMPONENT",
        "name": "TEST-ManualCollection-'"$(date +%s)"'",
        "state": "ENABLED",
        "creative": {
          "componentCreative": {
            "manualCollectionSettings": {
              "sharedSettings": {
                "brand": "'"${BRAND_NAME}"'"
              },
              "productInclusions": [
                { "productId": "B0EXAMPLE01", "productIdType": "ASIN" },
                { "productId": "B0EXAMPLE02", "productIdType": "ASIN" },
                { "productId": "B0EXAMPLE03", "productIdType": "ASIN" }
              ],
              "landingPage": {
                "landingPageType": "ASIN_LIST"
              }
            }
          }
        }
      }
    ]
  }' | jq .
```

#### Query Ads

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/ads" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["SPONSORED_BRANDS"]
    },
    "adGroupIdFilter": {
      "include": ["'"${AD_GROUP_ID}"'"]
    },
    "stateFilter": {
      "include": ["ENABLED", "PAUSED"]
    },
    "maxResults": 50
  }' | jq .
```


### 3.4 Target Operations

#### Create Keyword Target

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/targets" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "state": "ENABLED",
        "negative": false,
        "targetType": "KEYWORD",
        "bid": { "bid": 1.50 },
        "targetDetails": {
          "keywordTarget": {
            "keyword": "test keyword phrase",
            "matchType": "BROAD"
          }
        }
      }
    ]
  }' | jq .
```

#### Create Keyword Target (SP Global — Per-Marketplace Keyword Override)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/targets" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "marketplaceScope": "GLOBAL",
        "marketplaces": ["US", "GB", "DE"],
        "state": "ENABLED",
        "negative": false,
        "targetType": "KEYWORD",
        "bid": {
          "marketplaceSettings": [
            {"marketplace": "US", "currencyCode": "USD", "bid": 2.00},
            {"marketplace": "GB", "currencyCode": "GBP", "bid": 1.60},
            {"marketplace": "DE", "currencyCode": "EUR", "bid": 1.80}
          ]
        },
        "targetDetails": {
          "keywordTarget": {
            "keyword": "phone case",
            "matchType": "BROAD"
          }
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
      }
    ]
  }' | jq .
```

#### Create Negative Keyword Target

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/targets" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "state": "ENABLED",
        "negative": true,
        "targetType": "KEYWORD",
        "targetDetails": {
          "keywordTarget": {
            "keyword": "free cheap discount",
            "matchType": "EXACT"
          }
        }
      }
    ]
  }' | jq .
```

#### Create Product Target (ASIN)

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/targets" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [
      {
        "adGroupId": "'"${AD_GROUP_ID}"'",
        "adProduct": "SPONSORED_PRODUCTS",
        "state": "ENABLED",
        "negative": false,
        "targetType": "PRODUCT",
        "bid": { "bid": 2.00 },
        "targetDetails": {
          "productTarget": {
            "product": { "productId": "B0COMPETITOR1" },
            "productIdType": "ASIN",
            "matchType": "PRODUCT_EXACT"
          }
        }
      }
    ]
  }' | jq .
```

#### Query Targets

```bash
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/targets" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "adProductFilter": {
      "include": ["SPONSORED_PRODUCTS"]
    },
    "adGroupIdFilter": {
      "include": ["'"${AD_GROUP_ID}"'"]
    },
    "targetTypeFilter": {
      "include": ["KEYWORD"]
    },
    "negativeFilter": {
      "include": [false]
    },
    "maxResults": 100
  }' | jq .
```

---

## 4. Response Handling

### 4.1 Multi-Status Response Pattern (207)

All create/update/delete endpoints return this structure:

```json
{
  "success": [
    { "index": 0, "campaign": { "campaignId": "123456789", ... } }
  ],
  "error": [
    { "index": 1, "errors": [{ "code": "FIELD_VALUE_IS_INVALID", "message": "..." }] }
  ]
}
```

### 4.2 Extract IDs from Response

```bash
# Extract campaign ID from create response
CAMPAIGN_ID=$(curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  ... \
  | jq -r '.success[0].campaign.campaignId')

echo "Created campaign: $CAMPAIGN_ID"
```

### 4.3 Pagination

```bash
# Handle paginated query results
NEXT_TOKEN=""
PAGE=1

while true; do
  BODY='{"adProductFilter":{"include":["SPONSORED_PRODUCTS"]},"maxResults":100'
  if [[ -n "$NEXT_TOKEN" ]]; then
    BODY+=',"nextToken":"'"$NEXT_TOKEN"'"'
  fi
  BODY+='}'

  RESPONSE=$(curl -s -X POST "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
    -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
    -H "Content-Type: application/json" \
    -d "$BODY")

  COUNT=$(echo "$RESPONSE" | jq '.campaigns | length')
  echo "Page $PAGE: $COUNT campaigns"

  NEXT_TOKEN=$(echo "$RESPONSE" | jq -r '.nextToken // empty')
  if [[ -z "$NEXT_TOKEN" ]]; then
    break
  fi
  ((PAGE++))
done
```

---

## 5. Test Script Generator

### 5.1 Full CRUD Test Script Template

When asked to generate a test script, use this pattern:

```bash
#!/usr/bin/env bash
# Test: Full CRUD lifecycle for {RESOURCE}
set -euo pipefail

source .env  # Load credentials

BASE="${ADS_API_BASE}/adsApi/v1"
HEADERS=(
  -H "Authorization: Bearer ${ACCESS_TOKEN}"
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}"
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}"
  -H "Content-Type: application/json"
)

log() { echo -e "\n\033[1;34m>>> $1\033[0m"; }
ok()  { echo -e "\033[0;32m  ✓ $1\033[0m"; }
err() { echo -e "\033[0;31m  ✗ $1\033[0m"; exit 1; }

# --- CREATE ---
log "Creating {resource}..."
CREATE_RESP=$(curl -s -X POST "$BASE/create/{resources}" "${HEADERS[@]}" -d '{...}')
RESOURCE_ID=$(echo "$CREATE_RESP" | jq -r '.success[0].{resource}.{resource}Id // empty')
[[ -n "$RESOURCE_ID" ]] && ok "Created: $RESOURCE_ID" || err "Create failed: $CREATE_RESP"

# --- READ ---
log "Querying {resource}..."
QUERY_RESP=$(curl -s -X POST "$BASE/query/{resources}" "${HEADERS[@]}" \
  -d '{"adProductFilter":{"include":["SPONSORED_PRODUCTS"]},"{resource}IdFilter":{"include":["'"$RESOURCE_ID"'"]}}')
FOUND=$(echo "$QUERY_RESP" | jq '.{resources} | length')
[[ "$FOUND" -gt 0 ]] && ok "Found $FOUND result(s)" || err "Query returned empty"

# --- UPDATE ---
log "Updating {resource}..."
UPDATE_RESP=$(curl -s -X POST "$BASE/update/{resources}" "${HEADERS[@]}" \
  -d '{ "{resources}": [{ "{resource}Id": "'"$RESOURCE_ID"'", "state": "PAUSED" }] }')
UPDATE_OK=$(echo "$UPDATE_RESP" | jq '.success | length')
[[ "$UPDATE_OK" -gt 0 ]] && ok "Updated successfully" || err "Update failed: $UPDATE_RESP"

# --- DELETE ---
log "Deleting {resource}..."
DELETE_RESP=$(curl -s -X POST "$BASE/delete/{resources}" "${HEADERS[@]}" \
  -d '{ "{resource}Ids": ["'"$RESOURCE_ID"'"] }')
DELETE_OK=$(echo "$DELETE_RESP" | jq '.success | length')
[[ "$DELETE_OK" -gt 0 ]] && ok "Deleted successfully" || err "Delete failed: $DELETE_RESP"

echo -e "\n\033[1;32m=== All tests passed ===\033[0m"
```

### 5.2 Generating Commands from OpenAPI Spec

To generate curl commands for any endpoint in the spec:

1. **Find the endpoint** in `api-specs/unified-api-sp.json`, `api-specs/unified-api-sb.json`, `api-specs/unified-api-spglobal.json`, or `api-specs/unified-api-dsp.json`
2. **Extract the request schema** from `requestBody.content.application/json.schema.$ref`
3. **Resolve the schema** from `components.schemas.{SchemaName}`
4. **Identify required fields** from the `required` array
5. **Map enum values** from field definitions or from `api-specs/enums-unified-api.json`
6. **Generate minimal payload** with only required fields + valid enum values

**Spec selection guide:**
- SP single-marketplace campaigns → `unified-api-sp.json` (schemas prefixed `SP*`)
- SP Global multi-marketplace campaigns → `unified-api-spglobal.json` (schemas prefixed `SPGlobal*`)
- SB campaigns → `unified-api-sb.json` (schemas prefixed `SB*`)
- DSP campaigns → `unified-api-dsp.json` (schemas prefixed `DSP*`)

### 5.3 Quick Reference: jq Commands for Spec Exploration

```bash
SPEC="api-specs/unified-api-sp.json"

# List all endpoint paths
jq -r '.paths | keys[]' "$SPEC"

# List all operations for a specific path
jq '.paths["/adsApi/v1/create/campaigns"]' "$SPEC"

# Get the request body schema ref
jq -r '.paths["/adsApi/v1/create/campaigns"].post.requestBody.content["application/json"].schema["$ref"]' "$SPEC"

# Resolve a schema and list its properties
jq '.components.schemas.SPCreateCampaignsRequest.properties | keys' "$SPEC"

# Get required fields for a schema
jq '.components.schemas.SPCreateCampaign.required' "$SPEC"

# List all enum values for a specific field
jq '.components.schemas.AdProduct.enum' "$SPEC"

# Find all schemas containing a specific field name
jq '[.components.schemas | to_entries[] | select(.value.properties.bidStrategy?) | .key]' "$SPEC"
```

---

## 6. Common Test Scenarios

### 6.1 Validate Authentication

```bash
# Quick auth check — query campaigns (read-only, safe)
curl -s -o /dev/null -w "%{http_code}" -X POST \
  "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{"adProductFilter":{"include":["SPONSORED_PRODUCTS"]},"maxResults":1}'
# Expected: 200
```

### 6.2 Test Error Handling

```bash
# Missing required field (should return 400 with specific error)
curl -s -X POST "${ADS_API_BASE}/adsApi/v1/create/campaigns" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
  -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "campaigns": [{ "name": "Missing fields test" }]
  }' | jq '.error[0].errors'
# Expected: FIELD_VALUE_IS_NULL errors for adProduct, budgets, etc.
```

### 6.3 Test Rate Limiting

```bash
# Rapid fire to trigger 429
for i in $(seq 1 20); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${ADS_API_BASE}/adsApi/v1/query/campaigns" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Amazon-Ads-ClientId: ${CLIENT_ID}" \
    -H "Amazon-Ads-AccountId: ${ACCOUNT_ID}" \
    -H "Content-Type: application/json" \
    -d '{"adProductFilter":{"include":["SPONSORED_PRODUCTS"]},"maxResults":1}')
  echo "Request $i: HTTP $CODE"
  [[ "$CODE" == "429" ]] && echo "  ⚠️  Rate limited!" && break
done
```

---

## 7. Enum Values Quick Reference (for Test Payloads)

When generating test payloads, use these valid enum values:

| Field | Valid Values |
|-------|-------------|
| `adProduct` | `SPONSORED_PRODUCTS`, `SPONSORED_BRANDS` |
| `state` (create) | `ENABLED`, `PAUSED` |
| `costType` | `CPC`, `VCPM` |
| `marketplaceScope` | `SINGLE_MARKETPLACE` |
| `budgetType` | `MONETARY` |
| `recurrenceTimePeriod` | `DAILY`, `LIFETIME` |
| `bidStrategy` | `MANUAL`, `SALES_DOWN_ONLY`, `SALES_UP_AND_DOWN`, `RULE_BASED` |
| `placement` | `TOP_OF_SEARCH`, `PRODUCT_PAGE`, `REST_OF_SEARCH`, `HOME_PAGE` |
| `adType` (SP) | `PRODUCT_AD` |
| `adType` (SB) | `COMPONENT` |
| `targetType` | `KEYWORD`, `PRODUCT`, `PRODUCT_CATEGORY`, `THEME`, `AUTO` |
| `matchType` (keyword) | `BROAD`, `PHRASE`, `EXACT` |
| `matchType` (product) | `PRODUCT_EXACT`, `PRODUCT_SIMILAR` |
| `productIdType` | `ASIN`, `SKU` |
| `landingPageType` | `ASIN_LIST`, `STORE` |
| `kpi` (SB goal) | `CLICKS`, `TOP_OF_SEARCH_IMPRESSION_SHARE` |

> For the full authoritative enum list, see `api-specs/enums-unified-api.json` (generated by `scripts/extract-enums.sh`).

---

## 8. Batch Size Limits

Always respect these limits when generating test payloads:

| Resource | Create | Query (maxResults) | Update | Delete |
|----------|--------|-------------------|--------|--------|
| Campaigns | 10 | 100 | 10 | 10 |
| Ad Groups | 10 | 100 | 10 | 10 |
| Ads | 10 | 100 | 10 | 10 |
| Targets | 1000 | 5000 | 1000 | 1000 |

---

## 9. Troubleshooting Common Errors

| HTTP Code | Error | Likely Cause | Fix |
|-----------|-------|--------------|-----|
| 400 | `FIELD_VALUE_IS_NULL` | Missing required field | Add the field (check required list in spec) |
| 400 | `FIELD_VALUE_IS_INVALID` | Wrong enum value or format | Check enum tables in section 7 |
| 400 | `BAD_REQUEST` | Malformed JSON or wrong structure | Validate JSON syntax, check schema |
| 401 | `UNAUTHORIZED` | Token expired or invalid | Refresh OAuth2 token |
| 403 | `FORBIDDEN` | Wrong scope or no access | Check `advertising::campaign_management` scope |
| 429 | `TOO_MANY_REQUESTS` | Rate limited | Wait + exponential backoff |
| 500 | `INTERNAL_ERROR` | Server-side issue | Retry after 30s |
