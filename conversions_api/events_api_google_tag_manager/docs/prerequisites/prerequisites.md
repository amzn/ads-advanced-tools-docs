# Prerequisites

Before you begin implementing the Amazon Events API server-side templates, confirm that you have the following accounts, permissions, and credentials in place. Missing any of these will block your setup, so it's worth checking each item before diving into installation.

## Amazon Ads Setup

- Access to [Amazon Ads DSP](https://advertising.amazon.com/) with an advertiser account
- Events Manager terms accepted for your advertiser
- DSP user with Edit access to Events Manager (see [Granting Access to Events Manager](#granting-access-to-events-manager) below)
- An Amazon Developer [Login With Amazon (LWA)](https://advertising.amazon.com/API/docs/en-us/guides/get-started/retrieve-access-token) app created
- LWA Client ID, Client Secret, and Refresh Token obtained

If you need help generating a refresh token, refer to the [Amazon Ads API Postman guide](https://advertising.amazon.com/API/docs/en-us/guides/get-started/using-postman-collection).

## Google Setup

- A [Google Tag Manager](https://tagmanager.google.com/) web container deployed on your site
- A GTM server-side container provisioned (requires [Google Cloud](https://cloud.google.com/) access with billing enabled)
- Events flowing from your web container to your server container (e.g., via GA4, custom HTTP requests, or Measurement Protocol)

### Optional

- A [Google Analytics 4 (GA4)](https://support.google.com/analytics/answer/9304153) account — this guide uses GA4 as an example for sending events to the server container, but the template works with any event data source that forwards events to your GTM server container

## Granting Access to Events Manager

In order to access Events Manager, you must have at least "view and edit" permissions on the advertiser account's Events Manager. This access is scoped to a single advertiser account — advertisers do not need to grant full DSP console access.

To grant limited Events Manager access to a partner (agency or tech partner):

1. Navigate to Campaign Manager > Advertisers

   ![access 1](../../images/access%201.png)

2. Navigate to Account Access & Settings > Users inside the DSP account

   ![access 2](../../images/access%202.png)

3. Enter the user's email address, select **Custom Permissions**, and assign **View** and **Edit** permissions for **Events Manager**

   ![access 3](../../images/access%203.png)

4. Click **Send Invitation** to complete the process. The user must accept the invitation before they can access Events Manager.

---

Once you've confirmed all prerequisites, proceed to [Installation](../getting-started/installation.md) to import the templates into your GTM server container.
