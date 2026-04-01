# Validation & QA

After configuring the templates, you'll want to verify that events are flowing correctly from your website through GTM and into Amazon Ads. There are two stages of validation: confirming the data pipeline works in GTM's preview tools, and then verifying events land in Amazon's Events Manager.

## Validating in Google Tag Manager

GTM has two preview tools to verify your setup. It's best to open them simultaneously to see how site actions trigger web container tags and connect to server container tags. Your web container must be deployed on your website to send events to your server container.

### 1. Web Container Preview

Open Preview in your web container. This launches a new browser window of your website with the GTM web container attached. Here you can check if your tags fired, your dataLayer values, and the server_container_url where events are forwarded.

![validation 1](../../images/validation%201.png)

### 2. Server Container Preview

Open Preview in your server container. This launches a browser window showing a log of tag fires based on events forwarded from your web container. Here you validate whether the parameters in your CAPI tag are being retrieved from your web container as expected.

![validation 2](../../images/validation%202.png)

### 3. Trigger an Event

Navigate to a webpage where the tag connected to your server container is meant to fire. In the example below, an Add to Cart event tag is triggered by tags named "add_to_cart".

#### Web Container Preview Experience

Verify that your tag has fired successfully:

![validation 3](../../images/validation%203.png)

Confirm the attributes you configured in the tag are passed to your server container as expected (brand, customer_id, email, consent):

![validation 4](../../images/validation%204.png)

#### Server Container Preview Experience

The server container preview window shows the Events API tag firing with its parameters. The variables used in the tag parameters pull values directly from your website:

![validation 5](../../images/validation%205.png)

## Validating in Amazon Ads Console

Once events are flowing through GTM, confirm they're arriving in Amazon Ads by checking Events Manager.

1. Navigate to Campaign Manager > Advertisers

   ![validation 6](../../images/validation%206.png)

2. Select your advertiser from the list

3. Navigate to the "Events Manager" tab inside your advertiser account and view your event counts. Event counts are updated within two hours of a successful API request. The count shows total events before filtering (deduplication and consent opt-outs).

   ![validation 7](../../images/validation%207.png)

4. You can see the percentage of excluded events against the "Event count" (total unfiltered events) directly in Events Manager. This percentage represents events filtered out due to eventId-based deduplication and consent opt-outs.

If you do not have access to Events Manager, an admin on the DSP advertiser account can grant you limited permissions. See the [Prerequisites](../prerequisites/prerequisites.md#granting-access-to-events-manager) guide for instructions on granting Events Manager access.

---

If something isn't working as expected, see [Troubleshooting](../troubleshooting/troubleshooting.md) for common issues and fixes.
