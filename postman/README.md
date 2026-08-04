# Amazon Ads API Postman collections

[Postman](https://www.postman.com/) is a tool that allows developers to make API calls using a user interface. Postman can also store variables and perform basic automations that simplify API testing.

This folder contains **two separate Postman collections**, each with its own environment file. Pick the one that matches the interface you are building against.

Visit the [Amazon Ads advanced tools center](https://advertising.amazon.com/API/docs/en-us/) for additional API documentation.

## Which collection should I use?

| | **Amazon Ads API** | **Amazon Ads Unified API** |
|---|---|---|
| Collection file | `Amazon_Ads_API.postman_collection.json` | `Amazon_Ads_Unified_API.postman_collection.json` |
| Environment file | `Amazon_Ads_API_Environment.postman_environment.json` | `Amazon_Ads_Unified_API_Environment.postman_environment.json` |
| Interface | Per-service endpoints (`/sp/campaigns`, `/reporting/reports`, and so on) | Single unified interface under `/adsApi/v1` |
| Account context | `Amazon-Advertising-API-Scope: {{profileId}}` | `Amazon-Ads-AccountId: {{accountId}}` |
| Best for | Existing integrations, service-specific workflows | New integrations that want one consistent request pattern across ad products |

Both collections share the same OAuth setup and the same Auth folder behavior, so credentials you already have work for either one. You can import both and switch environments as needed.

---

## 1. Amazon Ads API collection

The original collection, organized by service. It includes commonly used endpoints plus pre- and post-request scripts that automate management of auth credentials.

**Files**

- `Amazon_Ads_API.postman_collection.json`
- `Amazon_Ads_API_Environment.postman_environment.json`

**Currently supported**

- Authentication
- GET profiles
- GET manager accounts
- Sponsored Products campaign management (version 3)
- Sponsored Brands campaign management (version 4)
- Sponsored ads reporting
    - Version 3
    - Version 2
- DSP reporting
- Sponsored ads snapshots
- Test accounts
- Amazon Marketing Stream
- Amazon Marketing Cloud
- Product metadata
- Sponsored ads budget usage
- Sponsored ads budget rules
- Sponsored Brands campaigns, ads, and ad groups
- Creative asset library
- Stores
- Locations
- Sponsored Display
- Sponsored TV
- Exports
- Partner opportunities

---

## 2. Amazon Ads Unified API collection

A collection for the Amazon Ads API unified interface (`/adsApi/v1`). Operations are grouped by resource rather than by ad product, and each request includes an example body where applicable.

**Files**

- `Amazon_Ads_Unified_API.postman_collection.json`
- `Amazon_Ads_Unified_API_Environment.postman_environment.json`

**Structure**

- **Auth** — auth grant login, access token from auth grant code, access token from refresh token
- **Unified API — Prod (3P)** — generally available resources, including Campaigns, AdGroups, Ads, Targets, AdAssociations, AdExtensions, AdvertisingDeals, AdvertisingDealTargets, Brand Stores (stores, pages, editions, publish versions), Commitments, CommitmentSpends, CampaignForecasts, Recommendations, RecommendationTypes, GeoLocations, LocationIndexes, BrandedKeywordsPricings, ReservedTargetPricings, and KeywordReservationValidations
- **Unified API — Beta** — preview resources, including Reports, Events, Rules, RuleLinks, Labels, AdvertiserAccounts, ManagerAccounts, SellingAccounts, AccountCombinationInvitations, BuyerSeats, Publishers, InventoryGroups, deal planning and access resources, supplier proposal and deal resources, and linear TV forecasting resources

Beta resources may change without notice. Treat them as unstable and avoid depending on them in production integrations.

**Environment notes**

This environment adds a few variables that the per-service environment does not use:

- `accountId` — sent as the `Amazon-Ads-AccountId` header on unified requests
- `api_url_na`, `api_url_eu`, `api_url_fe` — regional endpoints; copy the one you need into `api_url`

---

## Setup

To use either collection, you will need credentials for the Amazon Ads API. For information about acquiring credentials, see the [onboarding overview for the Amazon Ads API](https://advertising.amazon.com/API/docs/en-us/setting-up/overview).

1. Import the collection file and its matching environment file into Postman.
2. Select the imported environment.
3. Set `client_id` and `client_secret`.
4. Run the requests in the **Auth** folder to obtain an access token and refresh token.
5. Set the account context: `profileId` for the Amazon Ads API collection, or `accountId` for the Unified API collection.

Store `client_secret`, `access_token`, and `refresh_token` as Postman **secret** variable types, and keep them out of any exported files you share or commit.

Find detailed setup instructions in the Amazon Ads advanced tools center for:

- [New users of the Amazon Ads API](https://advertising.amazon.com/API/docs/en-us/getting-started/using-postman-collection)
- [Users with an existing refresh token](https://advertising.amazon.com/API/docs/en-us/tutorials/postman)

You can also view a [video walkthrough of the collection setup steps on YouTube](https://www.youtube.com/watch?v=SWqOPN33phw).

## Issues and support

For technical support for the Amazon Ads API, see [Technical support](https://advertising.amazon.com/API/docs/en-us/info/support) in the Amazon Ads advanced tools center.

To report a bug or suggest an improvement relating to these collections or the documentation, [create an issue](https://github.com/amzn/ads-advanced-tools-docs/issues/new/choose). Please mention which collection the issue applies to.
