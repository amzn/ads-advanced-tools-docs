# Amazon Ads MCP Workshop (Non-AWS)

Build an AI agent that manages Amazon Ads campaigns using the Model Context Protocol (MCP) and Claude via the Anthropic API. No AWS account required — everything runs locally.

## Prerequisites

- Python 3.13+
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com/))
- Amazon Ads API credentials (Client ID, Client Secret, Refresh Token)

## Modules

| Module | Title | Time |
|---|---|---|
| [01](01-overview-and-setup.md) | Overview and Setup | 15 min |
| [02](02-connect-ads-mcp-server.md) | Connect to Amazon Ads MCP Server | 20 min |
| [03](03-build-ads-agent.md) | Build an Ads Agent | 45 min |
| [04](04-testing-and-cleanup.md) | Testing and Cleanup | 20 min |

**Total estimated time: ~1.5 hours**

## What You Will Build

- OAuth authentication with the Amazon Ads MCP Server
- Tool discovery via Streamable HTTP (50+ advertising tools)
- A LangGraph agent powered by Claude that orchestrates Ads API calls
- Interactive chat for account discovery, campaign management, and reporting

## Architecture

```
┌─────────────────────┐
│    User Prompt       │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   LangGraph Agent    │
│   (runs locally)     │
└──────────┬──────────┘
           │
     ┌─────┴──────┐
     │             │
     ▼             ▼
┌──────────┐  ┌──────────────┐
│ Anthropic│  │  Amazon Ads  │
│   API    │  │  MCP Server  │
│ (Claude) │  │              │
└──────────┘  └──────────────┘
```

## Contributors

Created by [Chintan Sanghavi](https://www.linkedin.com/in/chintansanghavi/) and [Christelle Ngambula](https://www.linkedin.com/in/chrisngambula/), Senior Solutions Architects at Amazon Ads.

## Looking for the AWS version?

The [full AWS workshop](https://advertising.amazon.com/API/docs/en-us/bulksheets/hands-on-workshops/amazon-ads-mcp-server/08-build-ads-agent#step-4-create-the-ads-campaign-agent) covers Amazon Bedrock AgentCore deployment, custom MCP servers, Knowledge Bases, and more (~4 hours, 10 modules).
