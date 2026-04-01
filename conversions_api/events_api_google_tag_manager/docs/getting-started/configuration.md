# Configuration

Once the templates are imported, you need to create and configure the variables and tag that power the integration. This involves setting up OAuth authentication, timestamp formatting, and the main Events API tag with your event details, match keys, and consent settings.

## Step 1: Set Up Authentication Variable

1. Create a new variable using the **Amazon CAPI Auth** template
2. Configure the following fields:
   - **Client ID**: Your Amazon Ads API client ID
   - **Client Secret**: Your Amazon Ads API client secret
   - **Refresh Token**: Your Amazon Ads API refresh token

If you need help generating a refresh token, review the [Amazon Ads API Postman guide](https://advertising.amazon.com/API/docs/en-us/guides/get-started/using-postman-collection).

![config 1](../../images/config%201.png)

The auth variable automatically refreshes access tokens before expiration, caches tokens for 55 minutes with a 5-minute buffer, and handles token storage across multiple requests.

## Step 2: Set Up Timestamp Variable

1. Create a new variable using the **Amazon CAPI Timestamp** template
2. No configuration needed — it automatically generates the current timestamp in ISO format

   ![config 2](../../images/config%202.png)

## Step 3: Configure the Main Tag

Create a new tag using the **Amazon Events API** template and configure the following sections.

### Event Configuration

- **Amazon CAPI Auth Variable**: Select the auth variable created in Step 1
- **Account ID**: Your DSP Advertiser ID from your seat found in the Amazon Ads console

![config 3](../../images/config%203.png)

Your Account ID can be found in the DSP UI by navigating to Amazon DSP → Campaign Manager:

![config 4](../../images/config%204.png)

### Event Description

- **Event Name**: Descriptive name for this conversion event (e.g., Purchase_Complete, Newsletter_Signup, Contact_Form)
- **Conversion Type**: Select from the [supported conversion types](../overview/overview.md#template-components). "Other" can be used for custom conversions as a catch-all.
- **Event Source**: Website (default)
- **Dataset Name** (recommended): Groups related events for organization and reporting (e.g., website_events, mobile_app_events). Auto-generated if blank. Read more about datasets [here](https://advertising.amazon.com/API/docs/en-us/guides/events/events#the-datasets-concept).

In the example below, a `PAGE_VIEW` event named "Demo_Page_View" is created. This event is grouped into a "Website_Visits" dataset that can include events from other conversions as well.

![config 5](../../images/config%205.png)

---

Now that the tag is configured, see [Using the Template](using-the-template.md) to set up match keys, event data, consent, and deduplication.
