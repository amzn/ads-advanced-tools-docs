# Amazon Events API (CAPI v2) Server-Side Template for Google Tag Manager

The Amazon Events API Google Tag Manager server-side template collection allows you to implement Amazon Events API (Conversions API v2) on your website using Google Tag Manager's (GTM) server-side capabilities. This solution provides comprehensive event tracking and conversion attribution for Amazon Advertising through direct server-to-server integration.

## What's Included

> **Note:** The template code is shared upon engagement in a zip file format that includes the variable and tag templates needed to implement Amazon Events API using Google Tag Manager. If this implementation is of interest, please reach out to the [Amazon Ad Tech Solutions team](mailto:ats-custom-projects@amazon.com) or your dedicated Amazon Ad Tech Account Executive.

This solution contains three GTM templates — one tag and two utility variable templates — that work together to authenticate, format, and send conversion events to Amazon Ads:

| Template | File | Purpose |
|---|---|---|
| Amazon Events API Tag | `Amazon_Events_API_Tag_Template.tpl` | Sends conversion events to Amazon Events Manager |
| Amazon CAPI Auth Variable | `Amazon_CAPI_Auth_Variable.tpl` | Handles OAuth authentication with automatic token refresh |
| Amazon CAPI Timestamp Variable | `Amazon_CAPI_Timestamp_Variable.tpl` | Converts timestamps to ISO-8601 format |


## Architecture

The diagram below illustrates the end-to-end data flow: event sources send data into a GTM server container, where the Amazon Events API templates handle authentication, normalization, and delivery to Amazon Ads Events Manager via the Events API.

![Architecture](architecture/amazon-events-api-gtm.png)

For details on how to connect different event sources to a GTM server container, see Google's [Send data to server-side Tag Manager](https://developers.google.com/tag-platform/tag-manager/server-side/send-data) guide.

## Documentation

| Section | Description |
|---|---|
| [Overview](docs/overview/overview.md) | What the solution is, why server-side, who it's for, template components, and key features |
| [Prerequisites](docs/prerequisites/prerequisites.md) | Required accounts, credentials, and permissions before you start |
| [Installation](docs/getting-started/installation.md) | Importing templates into your GTM server container |
| [Configuration](docs/getting-started/configuration.md) | Setting up authentication, timestamps, and the main tag |
| [Using the Template](docs/getting-started/using-the-template.md) | Match keys, event data, custom attributes, consent, and deduplication |
| [Validation & QA](docs/validation/validation.md) | Verifying events in GTM preview and Amazon Ads Console |
| [Troubleshooting](docs/troubleshooting/troubleshooting.md) | Common issues and how to resolve them |

## Getting Started

1. Confirm you meet all [Prerequisites](docs/prerequisites/prerequisites.md)
2. Follow the [Installation](docs/getting-started/installation.md) guide to import the templates
3. Walk through [Configuration](docs/getting-started/configuration.md) and [Using the Template](docs/getting-started/using-the-template.md)
4. Validate your setup with the [Validation & QA](docs/validation/validation.md) guide

## Support

For issues, questions, or contributions, refer to the [Amazon Ads Events API documentation](https://advertising.amazon.com/API/docs/en-us/guides/events/events) or contact your Amazon Ads representative.

**Created by**: Justin Cartwright, Ad Tech Consultant, Amazon Ad Tech Solutions  
**Version**: v1.0  
**Last Updated**: 3/31/2026
