---
title: "Module 2: Connect to Amazon Ads MCP Server"
description: Obtain an OAuth access token, connect to the Amazon Ads MCP Server via Streamable HTTP, and discover available tools.
---

# Module 2: Connect to Amazon Ads MCP Server

>[NOTE] Exchange your credentials for an access token, connect to the Amazon Ads MCP Server, and explore the available tools.

## When to Use

- You have completed Module 1 and your environment is set up
- You have Amazon Ads API credentials (Client ID, Client Secret, Refresh Token)

---

## Step 1: Understand the Connection Flow

The Amazon Ads MCP Server is a remote endpoint you connect to via Streamable HTTP. The connection requires an OAuth access token obtained by exchanging your Refresh Token.

```
Your Code
    │
    ├── 1. Exchange Refresh Token for Access Token
    │       POST https://api.amazon.com/auth/o2/token
    │       → Returns short-lived Access Token
    │
    ├── 2. Connect to Ads MCP Server via Streamable HTTP
    │       https://advertising-ai.amazon.com/mcp
    │       Headers: Authorization, Amazon-Ads-ClientId
    │
    └── 3. Discover tools via tools/list
            → Returns available advertising tools
```

### MCP Server URLs by Region

| Region | Endpoint URL |
|--------|-------------|
| NA (North America) | `https://advertising-ai.amazon.com/mcp` |
| EU (Europe) | `https://advertising-ai-eu.amazon.com/mcp` |
| FE (Far East) | `https://advertising-ai-fe.amazon.com/mcp` |

---

## Step 2: Test the OAuth Token Exchange

Create a file called `test_connection.py` to verify your credentials work:

```python
"""Test OAuth token exchange and Ads MCP Server connection."""

import os
import asyncio
import requests
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")


def get_access_token() -> str:
    """Exchange refresh token for a short-lived access token."""
    response = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    if response.status_code != 200:
        print(f"OAuth failed: {response.status_code} - {response.text[:200]}")
        return ""
    print("Access token obtained successfully")
    return response.json()["access_token"]


async def discover_tools():
    """Connect to the Ads MCP Server and list available tools."""
    access_token = get_access_token()
    if not access_token:
        return

    client = MultiServerMCPClient(
        {
            "amzn-ads": {
                "url": "https://advertising-ai.amazon.com/mcp",
                "transport": "streamable_http",
                "headers": {
                    "Authorization": access_token,
                    "Amazon-Ads-ClientId": CLIENT_ID,
                    "Accept": "application/json, text/event-stream",
                },
            }
        }
    )

    tools = await client.get_tools()
    print(f"\nDiscovered {len(tools)} tools:\n")
    for tool in tools:
        print(f"  - {tool.name}")

    return tools


if __name__ == "__main__":
    asyncio.run(discover_tools())
```

Run it:

```bash
python test_connection.py
```

You should see a list of 50+ tools. If you get a 403 error, verify:
- Your Client ID, Client Secret, and Refresh Token are correct
- The `Authorization` header contains the raw access token (no `Bearer` prefix)
- Your application has the correct scopes in the Amazon Ads developer console

>[NOTE] The `Authorization` header must contain the raw access token, not `Bearer <token>`. This is a common source of 403 errors.

---

## Step 3: Understand the Tool Naming Convention

Amazon Ads MCP Server tools follow a `<tool_group>-<tool_name>` pattern:

| Tool Group | Description | Example Tools |
|---|---|---|
| **account_management** | Manage advertiser accounts and profiles | `account_management-list_profiles` |
| **campaign_management** | Create, update, and manage campaigns | `campaign_management-create_campaign`, `campaign_management-list_campaigns` |
| **reporting** | Generate performance reports | `reporting-get_campaign_report` |
| **locale_expansion** | Marketplace expansion recommendations | `locale_expansion-get_locale_recommendations` |

### Account Identification Parameters

Many tools require account identifiers:

| Parameter | Description |
|---|---|
| `profileId` | Identifies an Amazon Ads profile for a specific marketplace. Obtain via `account_management-list_profiles`. |
| `advertiserAccountId` | Identifies a specific advertiser account. |
| `managerAccountId` | Identifies the manager (agency) account. |

Typical workflow: call `query_advertiser_accounts` first to discover accounts, then use the returned identifiers in subsequent calls.

---

## Step 4: Filter Tools (Optional)

The Ads MCP Server exposes 50+ tools. You can filter to only the groups you need using the `TOOL_FILTER` environment variable you set in Module 1.

```python
tool_filter = os.getenv("TOOL_FILTER", "")
allowed_groups = [g.strip() for g in tool_filter.split(",") if g.strip()]

if allowed_groups:
    filtered_tools = [
        t for t in all_tools
        if any(t.name.startswith(g) for g in allowed_groups)
    ]
    print(f"Filtered to {len(filtered_tools)} tools from groups: {allowed_groups}")
else:
    filtered_tools = all_tools
```

>[TIP] Filtering tools reduces the token count in Claude's context window and improves response quality. Start with `account_management,campaign_management,reporting` and expand as needed.

---

## Summary

You have:
1. Exchanged your Refresh Token for an access token via the Amazon OAuth endpoint
2. Connected to the Amazon Ads MCP Server via Streamable HTTP
3. Discovered available tools and understood the naming convention

In the next module, you will build a LangGraph agent that uses these tools with Claude via the Anthropic API.

---

[← Previous: Overview and Setup](01-overview-and-setup.md) | [Next: Build an Ads Agent →](03-build-ads-agent.md)
