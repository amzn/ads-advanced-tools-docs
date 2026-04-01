# Using the Template

With the tag configured, this section covers how to populate event data, match keys, custom attributes, and consent settings. It also covers how to set up event deduplication to prevent duplicate conversions from being counted.

## Match Keys (At Least One Required)

Match keys are pieces of user identity information — like an email address or phone number — that Amazon uses to attribute conversion events back to ad impressions. When a user converts on your site, Amazon matches the hashed identity data you send against its own audience signals to determine whether that user was exposed to one of your ads. The more match keys you provide, the higher your match rate and the more complete your attribution picture becomes.

Match key values are passed from your website's dataLayer. The Amazon Events API requires PII fields to be normalized and hashed in SHA-256 following Amazon's [specifications](https://advertising.amazon.com/help/GCCXMZYCK4RXWS6C).

You can hash the values yourself upstream from your website, or leave them as-is. The tag template will handle normalization and hashing if you pass raw PII from your website to Google Tag Manager.

Match keys are configured as a table — select the type from the dropdown and provide the value. Click "Add Row" to include additional match keys.

![config 6](../../images/config%206.png)

In the example above, Email and Phone match keys are configured with GTM event variables. Values can be plain (e.g., "john@example.com") or pre-hashed SHA-256.

Supported match keys (see [API docs](https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events/operation/CreateEvent) for the latest list):

- **Email**: Normalized and hashed email address
- **Phone**: Normalized and hashed phone number
- **First Name / Last Name**: Normalized and hashed
- **Address / City / State / Postal**: Normalized and hashed
- **Mobile Ad ID (MAID)**: ADID/IDFA/FIREADID (sent as-is, no hashing)
- **RAMP_ID**: LiveRamp identifier (sent as-is, no hashing)
- **MATCH_ID**: Alphanumeric identifier, max 100 characters (sent as-is, no hashing)

## Event Data

For event data, here is an Off-Amazon Conversion example showing all the fields the template supports:

![config 7](../../images/config%207.png)

- **Country Code**: 2-letter format (US, GB, DE, etc.)
- **Event Time**: ISO format `YYYY-MM-DDThh:mm:ssZ` (e.g., 2025-11-07T15:30:00Z). Use the Amazon CAPI Timestamp variable for current time. Cannot be more than 21 days in the past.
- **Event Value**: Monetary value for purchases or custom scoring value for other events. Max 2 decimal places (e.g., 99.99, 1.50).
- **Currency Code**: Required for Off-Amazon Purchases. ISO-4217 format (USD, EUR, GBP, etc.) — only appears when Off-Amazon Purchases is selected.
- **Units Sold**: Number of items purchased (defaults to 1 if not provided) — only appears when Off-Amazon Purchases is selected.
- **Event ID**: Unique identifier for deduplication. Previously known as `clientDedupeID` in Conversions API v1. Pass a stable value (e.g., order ID, customer ID) from your website. See [Deduplication](#deduplication) below.

## Custom Attributes

With Events API, you can name your custom attributes. The three standard custom attributes still apply (brand, productId, and category), but you can also create named custom attributes with three supported data types: `STRING`, `INTEGER`, and `TIMESTAMP`.

- **Brand**: Product brand
- **Product ID**: Product identifier
- **Category**: Product category
- **Custom Data**: Additional key-value pairs with data type specification

![config 8](../../images/config%208.png)

## Privacy & Consent

The template supports multiple consent frameworks (TCF, GPP, and Amazon Consent). These consent values are captured when users interact with consent banners on your website—either through Consent Management Platform (CMP) integrations (third-party tools like OneTrust, Cookiebot, or Didomi that display and manage consent notices) or custom-built consent banners (self-developed consent collection interfaces). The consent choices users make are stored in your website's data layer, which GTM can then access and pass to the Amazon Events API. You must select one of the three provided frameworks that matches how your website collects consent. 

1. **TCF (Transparency & Consent Framework)**: IAB TCF v2.0 consent string (encoded).

   ![config 9a](../../images/config%209a.png)

2. **GPP (Global Privacy Platform)**: GPP consent string (encoded).

   ![config 9b](../../images/config%209b.png)

3. **Amazon Consent**: Direct Amazon consent signals:
   - **Ad Storage Consent** (optional): Whether the user has consented to cookie-based tracking
   - **User Data Consent** (required): Whether the user has consented to data processing for advertising

   ![config 9c](../../images/config%209c.png)

For Amazon Consent, values from your website are automatically normalized to GRANTED or DENIED from various formats:

| Consent Granted | Consent Denied |
|---|---|
| TRUE | FALSE |
| yes | no |
| 1 | 0 |
| on | off |
| enabled | disabled |
| opted_in | opted_out |
| optin | optout |
| granted | denied |

## Triggers

The Amazon Events API tag fires based on triggers you configure in your GTM server container. Triggers determine which incoming events should send a conversion to Amazon.

In the server container, create a Custom trigger with a condition on the `Event Name` built-in variable. For example, setting `Event Name` equals `purchase` will fire the tag only when a purchase event arrives from your web container. You can create separate triggers for each conversion type (page views, add to cart, purchases) or use a single trigger that matches multiple event names.

For more on configuring triggers in server-side GTM, see Google's [trigger documentation](https://support.google.com/tagmanager/answer/7679316).

## Deduplication

Amazon considers deduplication in two ways:

1. **Timestamp-based filtering**: When Amazon Ads receives multiple events from the same user for the same conversion (including event attributes) within a 200ms time window, only the first event is retained.
2. **eventId-based filtering**: The `eventId` parameter identifies the same conversion sent from multiple sources. For example, an event sent via both Events API and Amazon Ad Tag. When Amazon detects two conversions sharing the same `eventId`, the first (by timestamp) is retained. Ties are broken by source priority:
   1. Events API (Conversions API v2)
   2. Amazon Ad Tag

### Configuring eventId in Google Tag Manager

The `eventId` field is the deduplication field for Events API (previously `clientDedupeId`). You need to create dataLayer variables in your web container and forward them to your server container.

### Web Container Setup

1. Choose a variable for `eventId` — it needs a high level of uniqueness. A Transaction ID, customer ID, or GUID are all good options.
2. Store this value as a GTM DataLayer variable in your web container. In this example, a customer ID stored in the dataLayer is used:

   ![deduplication 1](../../images/deduplication%201.png)

3. Reference this value in your tag as a configuration parameter. This passes the value in the Events Stream to the server container. Note the parameter name for the next step.

   ![deduplication 2](../../images/deduplication%202.png)

### Server Container Setup

1. Create an event data variable that references the eventId event parameter. The "Key Path" should be `customer_id` or whatever key you used in your web container.

   ![deduplication 3](../../images/deduplication%203.png)

2. Reference this variable in the Events API template under the `eventId` field.

   ![deduplication 4](../../images/deduplication%204.png)

---

Once everything is configured, verify your setup end-to-end in [Validation & QA](../validation/validation.md).
