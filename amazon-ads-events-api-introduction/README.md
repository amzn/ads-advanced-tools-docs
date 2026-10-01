# Amazon Ads Events API - Python Integration Scripts

[![Amazon Ads](https://img.shields.io/badge/Amazon%20Ads-Events%20API%20v2-orange)](https://advertising.amazon.com/API/docs/en-us/guides/events/events)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)

Python scripts to help advertisers and integrators get started with the **Amazon Ads Events API** (Conversions API v2), a server-to-server integration for sending real-time or offline conversion events, bypassing browser-based tracking limitations.

📺 **[Video Walkthrough](https://youtu.be/nKxD_EyR1P8)**

---

## Key Capabilities

- **Automatic Conversion Definition Creation** — No pre-setup required
- **Dataset Organization** — Group events via Ads Data Manager (ADM)
- **Custom Attributes** — 10 named attributes + `brand`, `productId`, `category` (queryable in AMC)
- **Batch Processing** — Up to 500 events/request
- **Consent Support** — TCF, Amazon Consent String, GPP

---

## Prerequisites

1. **LwA Credentials** — Client ID, Client Secret, and Refresh Token
2. **Amazon Ads T&Cs** — Accept "Conversion Tracking" terms
3. **Profile ID & Advertiser ID** — Via Profile API and Advertisers API
4. **Permissions** — Event Manager Edit access

---

## The Intended Workflow 

1. **Set up** → User fills in `CLIENT_ID`, `CLIENT_SECRET`, and `REDIRECT_URI` in `.env`
2. **Run `get_access_token.py`** → Outputs `access_token` + `refresh_token` → User pastes `REFRESH_TOKEN` into `.env`
3. **Run `get_advertiser_accounts.py`** → Uses `REFRESH_TOKEN` to get a fresh `access_token` and retrieves `account_id` → User pastes both into `.env`
4. **Run `create_events.py`** → Uses `ACCESS_TOKEN` + `ACCOUNT_ID` + `CLIENT_ID` to send events

## Resource
📖 API Reference: https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events

