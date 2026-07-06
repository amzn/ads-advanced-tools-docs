# Amazon Ads Events API — Custom Attributes Sample Code

Working examples for sending custom attributes via the Amazon Ads Events API (Ads API v1).

**API Endpoint:** `POST /adsApi/v1/create/events`  
**API Docs:** [Events API Reference](https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events)

## Overview

This repository provides ready-to-run Python code for integrating custom attributes into your Amazon Ads conversion events. Each example maps to a real-world use case:

| Use Case | Script | Payload |
|----------|--------|---------|
| Lead Scoring | `src/examples/lead_scoring.py` | `payloads/lead_scoring.json` |
| Subscription Renewal & LTV | `src/examples/subscription_renewal.py` | `payloads/subscription_renewal.json` |
| Retail Margin Optimization | `src/examples/retail_margin.py` | `payloads/retail_margin.json` |
| Promotion & Discount Efficiency | `src/examples/promotion_efficiency.py` | `payloads/promotion_efficiency.json` |
| Customer Loyalty & Retention | `src/examples/customer_loyalty.py` | `payloads/customer_loyalty.json` |

## Prerequisites

- Python 3.9+
- An Amazon Ads developer account with API access
- OAuth 2.0 credentials (Client ID, Client Secret, Refresh Token)
- An Advertiser Account ID with Events API permissions

### Don't have API credentials yet?

If you haven't set up API access, follow the [Amazon Ads API Onboarding Guide](https://advertising.amazon.com/API/docs/en-us/guides/onboarding/overview) to:

1. **Create a Login with Amazon (LWA) application** — this gives you your `CLIENT_ID` and `CLIENT_SECRET`
2. **Register as a developer** on the Amazon Ads API portal
3. **Request API access** and get your application approved
4. **Generate a Refresh Token** via the OAuth 2.0 authorization flow
5. **Locate your Advertiser Account ID** in the Amazon Ads console

The onboarding guide walks through each step in detail. Once complete, you'll have all the credentials needed to run the examples below.

## Quickstart

### 1. Clone the repository

```bash
git clone https://github.com/amzn/ads-advanced-tools-docs.git
cd ads-advanced-tools-docs/custom-attributes-events-apis
```

### 2. Set up your environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```
CLIENT_ID=amzn1.application-oa2-client.your_client_id
CLIENT_SECRET=amzn1.oa2-cs.v1.your_client_secret
REFRESH_TOKEN=Atzr|your_refresh_token
ADVERTISER_ID=ENTITY_YOUR_ADVERTISER_ID
```

> **Note:** If you don't have these values, complete the [onboarding steps](https://advertising.amazon.com/API/docs/en-us/guides/onboarding/overview) first.

### 4. Run an example

Each example is a standalone script. Run any of them from the project root:

```bash
# Lead Scoring
python src/examples/lead_scoring.py

# Subscription Renewal & LTV
python src/examples/subscription_renewal.py

# Retail Margin Optimization
python src/examples/retail_margin.py

# Promotion & Discount Efficiency
python src/examples/promotion_efficiency.py

# Customer Loyalty & Retention
python src/examples/customer_loyalty.py
```

### 5. Verify the response

A successful run will output something like:

```
Access token refreshed successfully.
Sending event to NA endpoint...
Payload: {
  "events": [
    {
      "eventDescription": { ... },
      "countryCode": "US",
      "eventTime": "2026-07-06T14:30:00Z",
      "matchKeys": [ ... ],
      "value": 59.99,
      "currencyCode": "USD",
      "customData": [ ... ]
    }
  ]
}
Response: {
  "success": [
    { "event": { ... }, "index": 0 }
  ],
  "error": []
}

Event sent successfully! Index: 0
```

If you see errors, check:
- **401 Unauthorized** — Your refresh token may be expired. Generate a new one via the OAuth flow.
- **403 Forbidden** — Your account may not have `event_manager_view` or `event_manager_edit` permissions.
- **400 Bad Request** — Check that your `ADVERTISER_ID` and `dataSetName` are valid for your account.

## Project Structure

```
capi-custom-attributes-samples/
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt
├── LICENSE
├── src/
│   ├── __init__.py
│   ├── auth.py                 # OAuth 2.0 token management
│   ├── client.py               # Events API HTTP client
│   ├── send_event.py           # Core event building & submission logic
│   └── examples/
│       ├── __init__.py
│       ├── lead_scoring.py
│       ├── subscription_renewal.py
│       ├── retail_margin.py
│       ├── promotion_efficiency.py
│       └── customer_loyalty.py
├── payloads/
│   ├── lead_scoring.json
│   ├── subscription_renewal.json
│   ├── retail_margin.json
│   ├── promotion_efficiency.json
│   └── customer_loyalty.json
└── docs/
    └── custom_attributes_guide.md
```

## API Details

### Endpoint

```
POST https://advertising-api.amazon.com/adsApi/v1/create/events
```

Regional endpoints:
| Region | Base URL |
|--------|----------|
| North America | `https://advertising-api.amazon.com` |
| Europe | `https://advertising-api-eu.amazon.com` |
| Far East | `https://advertising-api-fe.amazon.com` |

### Required Headers

| Header | Description |
|--------|-------------|
| `Authorization` | `Bearer <access_token>` from OAuth 2.0 |
| `Amazon-Ads-AccountId` | Your Advertiser Account ID |
| `Amazon-Ads-ClientId` | Your LWA application Client ID |
| `Content-Type` | `application/json` |

### Required Permissions

The API requires one of: `event_manager_view` or `event_manager_edit`.

## Custom Attributes Reference

Amazon Ads Events API supports up to **13 custom attributes** per event:
- 10 fully custom attributes
- 3 reserved attributes: `brand`, `product`, `category`

Each attribute is defined with:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Attribute identifier. Only letters, numbers, and underscores allowed. |
| `dataType` | string | One of: `STRING`, `INTEGER`, `TIMESTAMP` |
| `value` | string | The attribute value. Only letters, numbers, and underscores allowed. |

### Conversion Types

| Value | Description |
|-------|-------------|
| `OFF_AMAZON_PURCHASES` | Purchase of a product or service |
| `LEAD` | Action that initiates a sales lead |
| `SIGN_UP` | Sign-up for a product or service |
| `SUBSCRIBE` | Subscription to a service |
| `APPLICATION` | Application submission |
| `ADD_TO_SHOPPING_CART` | Product added to cart |
| `PAGE_VIEW` | Page visit on your website |
| `SEARCH` | Product search |
| `CONTACT` | Contact information provided |
| `CHECKOUT` | Checkout page visit |
| `OTHER` | Actions not fitting standard types |

### Match Key Types

| Type | Description | Hashing |
|------|-------------|---------|
| `EMAIL` | Email address | SHA-256 (normalized, lowercased) |
| `PHONE` | Phone number | SHA-256 (digits + leading `+` only) |
| `FIRST_NAME` | First name | SHA-256 |
| `LAST_NAME` | Last name | SHA-256 |
| `ADDRESS` | Street address | SHA-256 |
| `CITY` | City | SHA-256 |
| `STATE` | State/region | SHA-256 |
| `POSTAL` | Postal/zip code | SHA-256 |
| `MAID` | Mobile Advertising ID (ADID, IDFA, FIREADID) | Raw value |
| `RAMP_ID` | LiveRamp RAMP ID | Raw value |
| `MATCH_ID` | Match ID | Raw value |

## Best Practices

- **Naming:** Use consistent `snake_case` — only letters, numbers, and underscores
- **Values:** Same character constraint as names — only letters, numbers, and underscores. For monetary values, use cents as integers (e.g., `"4999"` for $49.99)
- **Timing:** Send events as close to conversion time as possible
- **Batching:** Up to 500 events per API request
- **Deduplication:** Use `eventId` to prevent duplicate event processing
- **Hashing:** Always SHA-256 hash PII match keys (email, phone, name, address)
- **Selectivity:** Only include attributes that drive optimization or reporting decisions

## Error Handling

The API returns HTTP `207` (Multi-Status) with `success` and `error` arrays:

```json
{
  "success": [{ "event": {...}, "index": 0 }],
  "error": [{ "errors": [...], "index": 1 }]
}
```

The examples include handling for:
- Token expiration and automatic refresh
- Rate limiting (HTTP 429) with exponential backoff
- Server errors (HTTP 5xx) with retry logic
- Request timeouts

## Related Resources

- [Amazon Ads API Onboarding Guide](https://advertising.amazon.com/API/docs/en-us/guides/onboarding/overview)
- [Events API Reference (Ads API v1)](https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events)
- [Blog: Unlock Smarter Ad Optimization with Custom Attributes](https://advertising.amazon.com/blog/custom-attributes-capi)
- [Amazon Ads Developer Portal](https://advertising.amazon.com/API/docs/en-us)

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
