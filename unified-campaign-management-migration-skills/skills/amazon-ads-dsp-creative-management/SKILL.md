---
name: amazon-ads-dsp-creative-management
description: "Use when a developer needs to create and manage DSP creatives via the Unified API (/adsApi/v1/), including choosing the right creative type for inventory, building responsive ecommerce/video/audio/display/third-party ads, linking ads to ad groups via associations, and configuring tracking URLs."
---

# Amazon DSP Creative Management — Unified API Integration Guide

## 1. DSP Creative Landscape

The Unified API supports 5 ad types for DSP:

| `adType` | Creative Key | Inventory Types | Use Case |
|----------|-------------|-----------------|----------|
| `COMPONENT` | `componentCreative` | DISPLAY | Responsive ecommerce, asset-based, brand store ads |
| `VIDEO` | `videoCreative` | ONLINE_VIDEO, STREAMING_TV | Pre-roll, mid-roll, STV ads |
| `AUDIO` | `audioCreative` | AUDIO, PODCAST | Streaming audio and podcast ads |
| `DISPLAY` | `displayCreative` | DISPLAY | Custom image-based display ads |
| `THIRD_PARTY` | `thirdPartyCreative` | DISPLAY, ONLINE_VIDEO, STREAMING_TV | Creatives served from third-party ad servers |

### COMPONENT Sub-Types (Most Common)

The `COMPONENT` ad type has 3 settings variants:

| Settings Key | Description | Best For |
|-------------|-------------|----------|
| `responsiveEcommerceSettings` | Auto-generated from product catalog | Product advertising, performance campaigns |
| `assetBasedCreativeSettings` | Custom images + headlines + CTA | Brand awareness with custom visuals |
| `brandStoreSettings` | Links to Amazon Brand Store | Driving traffic to brand pages |

---

## 2. Creative Creation Flow

### Complete Lifecycle

```
1. Create Campaign (PAUSED)
2. Create Ad Group (PAUSED)
3. Create Ad ← creative content lives here
4. Create Ad Association ← links ad to ad group
5. Update Ad Group → ENABLED
6. Update Campaign → ENABLED
```

### Key Concepts

- **Ad** = creative content + metadata (no separate "creative" entity)
- **Ad Association** = the link between an ad and an ad group (with optional weight and schedule)
- One ad can be linked to **multiple ad groups** via multiple associations
- Ad associations have `state` (ENABLED/PAUSED); the ad itself does not
- `creativeRotationType` on the ad group controls how multiple ads rotate (RANDOM or WEIGHTED)

### Minimal Create Ad Request

```json
POST /adsApi/v1/create/ads
{
  "ads": [{
    "adProduct": "AMAZON_DSP",
    "adType": "COMPONENT",
    "name": "My Responsive Ad",
    "state": "PAUSED",
    "creative": {
      "componentCreative": {
        "responsiveEcommerceSettings": { ... }
      }
    }
  }]
}
```

### Minimal Create Ad Association

```json
POST /adsApi/v1/create/adAssociations
{
  "adAssociations": [{
    "adId": "AD_ID",
    "adGroupId": "AG_ID",
    "state": "ENABLED"
  }]
}
```

---

## 3. Inventory Type → Creative Type Decision Matrix

Use this to decide which `adType` and creative settings to use:

| Ad Group `inventoryType` | Recommended `adType` | Creative Settings | Notes |
|--------------------------|---------------------|-------------------|-------|
| `DISPLAY` | `COMPONENT` | `responsiveEcommerceSettings` | Best performance for product ads |
| `DISPLAY` | `COMPONENT` | `assetBasedCreativeSettings` | Custom brand visuals |
| `DISPLAY` | `COMPONENT` | `brandStoreSettings` | Drive to Brand Store |
| `DISPLAY` | `DISPLAY` | `standardDisplaySettings` | Simple custom image ads |
| `DISPLAY` | `THIRD_PARTY` | `thirdPartyDisplaySettings` | Externally hosted display |
| `ONLINE_VIDEO` | `VIDEO` | `onlineVideoSettings` | Pre-roll / mid-roll video |
| `ONLINE_VIDEO` | `THIRD_PARTY` | `thirdPartyVideoSettings` | Externally hosted video (VAST) |
| `STREAMING_TV` | `VIDEO` | `streamingTvSettings` | Connected TV / Fire TV |
| `AUDIO` | `AUDIO` | `standardAudioSettings` | Music streaming ads |
| `PODCAST` | `AUDIO` | `standardAudioSettings` | Podcast ads |

> ⚠️ Mismatching inventory type and creative type will cause delivery failures.

---

## 4. Component Creative (Most Common)

### 4a. Responsive Ecommerce

The most popular DSP creative format. Amazon auto-generates multiple ad variations from your product catalog.

**Required fields:**
- `language` — ISO locale (e.g., `"EN"`)
- `inventoryTypes` — `["DISPLAY"]` or `["DISPLAY", "NATIVE"]`
- `products` — at least 1 product (`productId` + `productIdType`)
- `optimizationGoalKpi` — `CLICK_THROUGH_RATE`, `DETAIL_PAGE_VIEW_RATE`, or `PURCHASE_RATE`
- `responsiveSizingBehavior` — `"ENABLED"` or `"DISABLED"`
- `supportedThirdPartySellers` — `"ENABLED"` or `"DISABLED"`

**Example:**

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "COMPONENT",
  "name": "Holiday Sale - Responsive Ecommerce",
  "state": "PAUSED",
  "creative": {
    "componentCreative": {
      "responsiveEcommerceSettings": {
        "language": "EN",
        "inventoryTypes": ["DISPLAY"],
        "products": [
          {"productId": "B0EXAMPLE01", "productIdType": "ASIN"},
          {"productId": "B0EXAMPLE02", "productIdType": "ASIN"},
          {"productId": "B0EXAMPLE03", "productIdType": "ASIN"}
        ],
        "optimizationGoalKpi": "CLICK_THROUGH_RATE",
        "responsiveSizingBehavior": "ENABLED",
        "supportedThirdPartySellers": "DISABLED",
        "headlines": "Holiday Deals - Up to 30% Off",
        "impressionTrackingUrls": [
          {"url": "https://tracking.example.com/imp?id=123"}
        ]
      }
    }
  }
}
```

**Optional enhancements:**
- `headlines` — custom headline text
- `disclaimers` — legal disclaimer
- `images` — custom images (up to 3, with square/tall/wide formats)
- `logos` — brand logo
- `creativeSizes` — specific placement sizes
- `recAdVariations` — `ADD_TO_CART`, `COUPON`, `CUSTOMER_REVIEWS`, `SHOP_NOW`
- `creativePropertiesToOptimize` — `HEADLINE` (let Amazon optimize)

### 4b. Asset-Based Creative

For custom brand visuals with full creative control.

**Required fields:**
- `language`, `inventoryTypes`
- `headlines` — array of headline strings (1-5)
- `squareImages`, `tallImages`, `wideImages` — custom images (assetId + assetVersion)
- `brand` — brand name string
- `callToActions` — CTA with URL
- `optimizationGoalKpi`
- `responsiveSizingBehavior`

**Example:**

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "COMPONENT",
  "name": "Brand Awareness - Asset Based",
  "state": "PAUSED",
  "creative": {
    "componentCreative": {
      "assetBasedCreativeSettings": {
        "language": "EN",
        "inventoryTypes": ["DISPLAY"],
        "brand": "MyBrand",
        "headlines": ["Discover Our New Collection"],
        "squareImages": [{"assetId": "ast-square-001", "assetVersion": "1"}],
        "tallImages": [{"assetId": "ast-tall-001", "assetVersion": "1"}],
        "wideImages": [{"assetId": "ast-wide-001", "assetVersion": "1"}],
        "callToActions": {
          "assetBasedCreativeCallToActionSettings": {
            "url": "https://www.amazon.com/stores/mybrand",
            "callToActionType": ["SHOP_NOW"]
          }
        },
        "optimizationGoalKpi": "CLICK_THROUGH_RATE",
        "responsiveSizingBehavior": "ENABLED"
      }
    }
  }
}
```

### 4c. Brand Store

Similar to asset-based but drives traffic to your Amazon Brand Store page.

```json
"creative": {
  "componentCreative": {
    "brandStoreSettings": {
      "language": "EN",
      "inventoryTypes": ["DISPLAY"],
      "brand": "MyBrand",
      "headlines": ["Visit Our Store"],
      "squareImages": [{"assetId": "ast-001", "assetVersion": "1"}],
      "tallImages": [{"assetId": "ast-002", "assetVersion": "1"}],
      "wideImages": [{"assetId": "ast-003", "assetVersion": "1"}],
      "callToActions": {
        "brandStoreCallToActionSettings": {
          "url": "https://www.amazon.com/stores/page/STORE_PAGE_ID",
          "callToActionType": ["SHOP_NOW"]
        }
      },
      "optimizationGoalKpi": "CLICK_THROUGH_RATE",
      "responsiveSizingBehavior": "ENABLED"
    }
  }
}
```

---

## 5. Video Creative

### Online Video (OLV)

For pre-roll/mid-roll video ads on web and mobile video inventory.

**Required fields:**
- `language`
- `videos` — video asset (`assetId` + `assetVersion`)

**Example:**

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "VIDEO",
  "name": "Product Launch - OLV 30s",
  "state": "PAUSED",
  "creative": {
    "videoCreative": {
      "onlineVideoSettings": {
        "language": "EN",
        "videos": {"assetId": "vid-001", "assetVersion": "1"},
        "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
        "callToActions": [
          {
            "learnMoreVideoCallToActionSettings": {
              "url": "https://www.amazon.com/dp/B0EXAMPLE",
              "position": "RIGHT"
            }
          }
        ],
        "impressionTrackingUrls": [
          {"url": "https://tracking.example.com/video-imp"}
        ]
      }
    }
  }
}
```

### Streaming TV (STV)

For Connected TV / Fire TV inventory. Note: interactive CTAs are limited compared to OLV.

```json
"creative": {
  "videoCreative": {
    "streamingTvSettings": {
      "language": "EN",
      "videos": {"assetId": "vid-stv-001", "assetVersion": "1"},
      "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
      "impressionTrackingUrls": [
        {"url": "https://tracking.example.com/stv-imp"}
      ]
    }
  }
}
```

---

## 6. Display Creative

Standard custom image display ads.

**Required fields:**
- `adChoicesPosition` — `TOP_LEFT`, `TOP_RIGHT`, `BOTTOM_LEFT`, `BOTTOM_RIGHT`
- `creativeSizes` — at least 1 size (width + height)
- `customImages` — at least 1 image (assetId + assetVersion)
- `language`

**Example:**

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "DISPLAY",
  "name": "Custom Banner 300x250",
  "state": "PAUSED",
  "creative": {
    "displayCreative": {
      "standardDisplaySettings": {
        "adChoicesPosition": "TOP_RIGHT",
        "language": "EN",
        "creativeSizes": [{"width": 300, "height": 250}],
        "customImages": [{"assetId": "img-banner-001", "assetVersion": "1"}],
        "impressionTrackingUrls": [
          {"url": "https://tracking.example.com/display-imp"}
        ]
      }
    }
  }
}
```

---

## 7. Audio Creative

For streaming audio and podcast inventory.

**Required fields:**
- `audio` — audio asset (assetId + assetVersion)
- `companionImages` — companion display image
- `headlines` — max 20 characters
- `language`

**Example:**

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "AUDIO",
  "name": "Audio Ad - Summer Promo",
  "state": "PAUSED",
  "creative": {
    "audioCreative": {
      "standardAudioSettings": {
        "language": "EN",
        "audio": {"assetId": "audio-001", "assetVersion": "1"},
        "companionImages": {"assetId": "companion-img-001", "assetVersion": "1"},
        "headlines": "Summer Sale Now On",
        "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}]
      }
    }
  }
}
```

---

## 8. Third-Party Creative

For creatives hosted on external ad servers.

### Third-Party Display

```json
{
  "adProduct": "AMAZON_DSP",
  "adType": "THIRD_PARTY",
  "name": "3P Display Ad",
  "state": "PAUSED",
  "creative": {
    "thirdPartyCreative": {
      "thirdPartyDisplaySettings": {
        "adChoicesPosition": "TOP_RIGHT",
        "language": "EN",
        "creativeSizes": [{"width": 728, "height": 90}],
        "thirdPartyTagHostingSource": "<script src='https://adserver.example.com/tag.js'></script>"
      }
    }
  }
}
```

### Third-Party Video (VAST)

```json
"creative": {
  "thirdPartyCreative": {
    "thirdPartyVideoSettings": {
      "language": "EN",
      "vastUrl": "https://adserver.example.com/vast?id=12345"
    }
  }
}
```

### Publisher Hosted (Google Ad Manager)

```json
"thirdPartyDisplaySettings": {
  "adChoicesPosition": "TOP_RIGHT",
  "language": "EN",
  "publisherHostedCreativeSource": "GOOGLE_AD_MANAGER"
}
```

---

## 9. Ad Association Management

### Create Association

```json
POST /adsApi/v1/create/adAssociations
{
  "adAssociations": [{
    "adId": "AD_123",
    "adGroupId": "AG_456",
    "state": "ENABLED"
  }]
}
```

### Weighted Rotation

When `creativeRotationType: "WEIGHTED"` on the ad group:

```json
{
  "adAssociations": [
    {"adId": "AD_1", "adGroupId": "AG_456", "state": "ENABLED", "weight": 70},
    {"adId": "AD_2", "adGroupId": "AG_456", "state": "ENABLED", "weight": 30}
  ]
}
```

### Scheduled Association (Flight-Level)

```json
{
  "adAssociations": [{
    "adId": "AD_123",
    "adGroupId": "AG_456",
    "state": "ENABLED",
    "startDateTime": "2026-01-01T00:00:00Z",
    "endDateTime": "2026-01-15T23:59:59Z"
  }]
}
```

### One Ad → Multiple Ad Groups

```json
{
  "adAssociations": [
    {"adId": "AD_123", "adGroupId": "AG_DISPLAY", "state": "ENABLED"},
    {"adId": "AD_123", "adGroupId": "AG_MOBILE", "state": "ENABLED"}
  ]
}
```

---

## 10. Tracking & Measurement

### Impression Tracking

All creative settings support `impressionTrackingUrls`:

```json
"impressionTrackingUrls": [
  {"url": "https://tracker1.example.com/imp?campaign=123"},
  {"url": "https://tracker2.example.com/pixel.gif?event=imp"}
]
```

Maximum: 5 URLs per creative.

### Click Tracking

Component and Display creatives support `clickTrackingUrls`:

```json
"clickTrackingUrls": [
  {"url": "https://tracker.example.com/click?id=456"}
]
```

Maximum: 5 URLs per creative.

### Video Call-to-Actions with Tracking

OLV videos support interactive CTAs that combine tracking with engagement:

| CTA Type | Key | Use Case |
|----------|-----|----------|
| Learn More | `learnMoreVideoCallToActionSettings` | Drive to URL with overlay |
| Click to URL | `clickToUrlVideoCallToActionSettings` | Direct link on click |
| Add to Cart | — | In-video shopping (STV only) |

---

## 11. Common Patterns & Best Practices

### Pattern A: Single Product Responsive Ecommerce

The simplest and most common DSP creative setup:

```json
// 1. Create Ad
POST /adsApi/v1/create/ads
{
  "ads": [{
    "adProduct": "AMAZON_DSP",
    "adType": "COMPONENT",
    "name": "Product X - Responsive",
    "state": "PAUSED",
    "creative": {
      "componentCreative": {
        "responsiveEcommerceSettings": {
          "language": "EN",
          "inventoryTypes": ["DISPLAY"],
          "products": [{"productId": "B0MYPRODUCT", "productIdType": "ASIN"}],
          "optimizationGoalKpi": "CLICK_THROUGH_RATE",
          "responsiveSizingBehavior": "ENABLED",
          "supportedThirdPartySellers": "DISABLED"
        }
      }
    }
  }]
}

// 2. Associate to Ad Group
POST /adsApi/v1/create/adAssociations
{
  "adAssociations": [{
    "adId": "RETURNED_AD_ID",
    "adGroupId": "MY_AG_ID",
    "state": "ENABLED"
  }]
}
```

### Pattern B: Multi-Product with Custom Headlines

```json
"responsiveEcommerceSettings": {
  "language": "EN",
  "inventoryTypes": ["DISPLAY", "NATIVE"],
  "products": [
    {"productId": "B0PROD1", "productIdType": "ASIN"},
    {"productId": "B0PROD2", "productIdType": "ASIN"},
    {"productId": "B0PROD3", "productIdType": "ASIN"}
  ],
  "headlines": "Top Picks for You",
  "recAdVariations": ["SHOP_NOW", "ADD_TO_CART", "CUSTOMER_REVIEWS"],
  "optimizationGoalKpi": "PURCHASE_RATE",
  "responsiveSizingBehavior": "ENABLED",
  "supportedThirdPartySellers": "DISABLED"
}
```

### Pattern C: OLV Video with Learn More CTA

```json
"videoCreative": {
  "onlineVideoSettings": {
    "language": "EN",
    "videos": {"assetId": "vid-30s-product", "assetVersion": "1"},
    "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
    "callToActions": [{
      "learnMoreVideoCallToActionSettings": {
        "url": "https://www.amazon.com/dp/B0EXAMPLE",
        "position": "RIGHT"
      }
    }]
  }
}
```

### Pattern D: Streaming TV Campaign

STV creatives are simpler — no interactive CTAs, focus on brand reach:

```json
"videoCreative": {
  "streamingTvSettings": {
    "language": "EN",
    "videos": {"assetId": "vid-stv-15s", "assetVersion": "1"},
    "products": [{"productId": "B0EXAMPLE", "productIdType": "ASIN"}],
    "impressionTrackingUrls": [{"url": "https://track.example.com/stv"}]
  }
}
```

### Pattern E: A/B Testing with Weighted Rotation

Create 2 ads, associate both to same ad group with different weights:

```json
// Create both ads (separate calls or batch)
// Then associate:
POST /adsApi/v1/create/adAssociations
{
  "adAssociations": [
    {"adId": "AD_VARIANT_A", "adGroupId": "AG_123", "state": "ENABLED", "weight": 50},
    {"adId": "AD_VARIANT_B", "adGroupId": "AG_123", "state": "ENABLED", "weight": 50}
  ]
}
```

> Ensure the ad group has `creativeRotationType: "WEIGHTED"` set.

---

## 12. FAQ

**Q: Do I need to upload assets before creating an ad?**
A: Yes. Use the Creative Assets API to upload images/videos/audio and get `assetId` + `assetVersion`. Then reference them in your ad creative.

**Q: Can I update a creative after it's associated to an ad group?**
A: Yes. Use `POST /adsApi/v1/update/ads` to modify the creative content. Changes apply to all ad groups the ad is associated with.

**Q: How do I remove an ad from an ad group?**
A: Delete the ad association: `POST /adsApi/v1/delete/adAssociations` with the association ID.

**Q: What's the batch limit for creating ads?**
A: Maximum 10 ads per request.

**Q: Can I use the same video for both OLV and STV?**
A: You need separate ad objects — one with `onlineVideoSettings` and one with `streamingTvSettings` — even if they reference the same video asset. They target different inventory types.

**Q: How do I know my creative was approved for delivery?**
A: Creative moderation is handled outside the Unified API. Use the legacy `/dsp/v1/adCreatives/moderations/list` endpoint to check moderation status, or check the DSP console.

**Q: What's the difference between `ENABLED` and `DISABLED` for `responsiveSizingBehavior`?**
A: `ENABLED` allows Amazon to automatically generate multiple ad sizes from your assets. `DISABLED` limits to your specified `creativeSizes` only.
