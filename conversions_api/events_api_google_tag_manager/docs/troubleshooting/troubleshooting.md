# Troubleshooting

If events aren't making it to Amazon Ads, the issue is usually one of a few common problems: authentication misconfiguration, missing match keys, or timestamp formatting. This section walks through how to identify and resolve each one.

## Authentication Failures

No outbound request will be made to Amazon if the authorization is incorrectly configured. To verify, navigate to Server Container > Preview > Tags.

Tags Fired will indicate a failure:

![troubleshooting 1](../../images/troubleshooting%201.png)

You can check the failure message directly in the Console tab:

![troubleshooting 2](../../images/troubleshooting%202.png)

If you see auth errors, double-check your Client ID, Client Secret, and Refresh Token in the Amazon CAPI Auth variable. Refer to the [Amazon Ads API Postman guide](https://advertising.amazon.com/API/docs/en-us/guides/get-started/using-postman-collection) to regenerate credentials if needed.

## Missing Match Keys

The Events API requires at least one match key (email, phone, first name, last name, etc.) to be provided. If none are configured, the tag will fail:

![troubleshooting 3](../../images/troubleshooting%203.png)

Ensure at least one match key variable is populated and pulling correctly from your website's dataLayer.

## Invalid Timestamps

Use the Amazon CAPI Timestamp variable to ensure proper ISO formatting. While the API accepts invalid timestamps with a 207 success response, events with incorrect timestamp formats will be marked as "BAD_REQUEST" in the response body.

![troubleshooting 4](../../images/troubleshooting%204.png)

This means you can receive a successful HTTP status but still have failed events if you don't check the response body. Using the CAPI Timestamp variable prevents this by automatically formatting timestamps correctly.

## Invalid DataLayer and Event Variable Configuration

If values from your website aren't reaching the server container, the issue is usually in how dataLayer variables are configured and forwarded. The methodology used to create the `eventId` variables in the [Deduplication](../getting-started/using-the-template.md#deduplication) section applies whenever you're passing attributes from web container to server container:

1. Create a DataLayer variable in your web container
2. Reference it as a configuration parameter in your tag
3. Create an event data variable in your server container that maps to the same key

## Support

For issues, questions, or contributions, refer to the [Amazon Ads Events API documentation](https://advertising.amazon.com/API/docs/en-us/guides/events/events) or contact your Amazon Ads representative. Security concerns can be raised to the Amazon Ad Tech Solutions team for template enhancement consideration.
