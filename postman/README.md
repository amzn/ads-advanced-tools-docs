# Amazon Ads API Postman collection

[Postman](https://www.postman.com/) is a tool that allows developers to make API calls using a user interface. Postman can also store variables and perform basic automations that simplify API testing. 

Amazon Ads provides a Postman collection that includes commonly used API endpoints. The collection also includes pre- and post-request scripts to automate management of auth credentials.

Visit the [Amazon Ads Advanced Tools Center](https://advertising.amazon.com/API/docs/en-us/) for additional API documentation.

## Currently supported

The Amazon Ads API Postman collection currently supports the following features:

- Authentication
- Sponsored ads reporting
- Sponsored ads snapshots 
- Test accounts
- GET Sponsored Products campaigns

## Setup

### Before you begin

Decide what version of Postman you want to use. If you don't have a Postman account, you can [download the desktop application](https://www.postman.com/downloads/). You can also [create a free account](https://identity.getpostman.com/signup) and use the web application. 

The following are required credentials for using this collection:

*   The **client ID** and **client secret** of a Login with Amazon client application approved to use the Amazon Ads API
*   An existing refresh token for the Amazon Ads API **or** Login credentials for an Amazon user account that manages Amazon Ads accounts.

For information about acquiring credentials, see the [onboarding overview for the Amazon Ads API](https://advertising.amazon.com/API/docs/en-us/setting-up/overview).

### Steps

To set up using an existing refresh token, follow the steps below. 

To instead set up using login credentials for an account that manages Amazon Ads accounts, see [Getting started using the Postman collection](https://advertising.amazon.com/API/docs/en-us/setting-up/using-postman-collection) in the Advanced Tools Center.

**File import and environment setup**

1. Download the Postman [environment file](https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/Amazon_Ads_API_Environment.postman_environment.json) and [collection file](https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/Amazon_Ads_API.postman_collection.json) from this repository.

2. [Import both files into Postman](https://learning.postman.com/docs/getting-started/importing-and-exporting-data/).

    - In the **Collections** tab, you should see the **Amazon Ads API** collection. 

    - In the **Environments** tab, you should see the **Amazon Ads API Environment**. 

3. From the **Environments** tab, select the **Amazon Ads API Environment**. 

4. Add your auth credentials to the environment. Enter your credentials into the **Current value** column for the following variables:

    | Variable | Description |
    |----------|-------------|
    | `client_id` | The client ID associated with your Login with Amazon application. |
    | `client_secret` | The client ID associated with your Login with Amazon application. |
    | `refresh_token` | Your refresh token used to get a fresh access token. **NOTE:** |

5. (Optional) Update the URL prefixes. In the downloaded file, `api_url` is populated with the North America URL. If the advertising account you are working with located outside North America, update the `api_url`. See the [API overview](https://advertising.amazon.com/API/docs/en-us/info/api-overview#api-endpoints) for available URLs by geography. 

6. Save your changes to the environment.

**Get a fresh access token**

7. From the Postman **Collections** tab, navigate to the **Amazon Ads API** collection.
8. [Set the active environment](https://learning.postman.com/docs/sending-requests/managing-environments/#selecting-an-active-environment) to **Amazon Ads API Environment**.
9. Open the **POST Access token from refresh token** endpoint. Mouse over the `{{refresh_token}}`, `{{client_id}`, and `{{client_secret}}` values to check that they are pulling in the right values from your environment. 
10. Send the request. You should get a `200 OK` response code. 
11. Navigate back to your **Amazon Ads API Environment**. You should see a new **Current value** for `access_token`, along with a timestamp in **Current value** for `token_expires_at`. A pre-request script in this collection uses this value to automatically refresh the access token as necessary.

**Get a profile ID**

12. Use the **Profiles** > **`GET` Profiles** endpoint to return profiles associated to your account. The first profile returned is added to your environment automatically. If you have multiple profile IDs, make sure to copy and paste the desired profile ID into your environment.

Now that your environment has a valid access token and profile ID, you are ready to make calls to the Amazon Ads API!

## Environment variable definitions

This table contains descriptions for all variables in the environment file. 

| Variable | Description |
|----------|-------------|
| `client_id` | The client ID of an authorized Login with Amazon application. |
| `client_secret` | The client secret of an authorized Login with Amazon application. |
| `permission_scope` | The scope used to determine permissions for your account. For general API use, keep the default value as `advertising::campaign_management`. |
| `redirect_uri` | Enter the URL included in the **Allowed Return URLs** configuration of a Login with Amazon application. Set to `https://amazon.com` by default; enable this URL in Login with Amazon. |
| `access_token` | The access token used to authenticate your API calls. Each token is valid for 60 minutes. |
| `refresh_token` | The token used to refresh your access token. |
| `token_expires_at` | The timestamp at which an access token expires. |
| `profileId` | The profile ID associated with your Amazon Ads account. |
| `auth_grant_url` | The URL prefix used for auth calls. |
| `token_url` | The URL prefix used for token calls. |
| `api_url` | The URL prefix used for general API calls. |

## Issues and support

For technical support for the Amazon Ads API, see [Technical support](https://advertising.amazon.com/API/docs/en-us/info/support) in the Amazon Ads Advanced Tools Center.

For issues relating to this collection or the documentation, please [create an issue in the `amzn/ads-advanced-tools-docs` repository](https://github.com/amzn/ads-advanced-tools-docs/issues/new/choose).
