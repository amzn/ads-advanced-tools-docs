---
title: "Module 3: Build an Ads Agent"
description: Build a LangGraph agent that connects to the Amazon Ads MCP Server and uses Claude via the Anthropic API to orchestrate advertising workflows.
---

# Module 3: Build an Ads Agent

>[NOTE] Build a LangGraph agent powered by Claude (via Anthropic API) that discovers and invokes Amazon Ads MCP Server tools to manage advertising campaigns.

## When to Use

- You have completed Module 2 and can connect to the Amazon Ads MCP Server
- You want to build an agent that orchestrates Ads API calls using natural language

---

## Step 1: Understand the Agent Architecture

```
┌─────────────────────┐
│    User Prompt       │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   LangGraph Agent    │
│                      │
│  ┌───────────────┐   │
│  │ Claude (LLM)  │   │     Streamable HTTP (MCP)
│  │ via Anthropic │   │────────────────────────────┐
│  │    API        │   │                            │
│  └───────────────┘   │                            ▼
└──────────────────────┘                   ┌──────────────┐
                                           │  Amazon Ads  │
                                           │  MCP Server  │
                                           └──────┬───────┘
                                                  │
                                                  ▼
                                           ┌──────────────┐
                                           │  Amazon Ads  │
                                           │     API      │
                                           └──────────────┘
```

The agent:
1. Receives a natural language prompt from the user
2. Uses Claude to decide which Ads MCP tools to call
3. Executes tool calls against the Amazon Ads MCP Server
4. Returns a natural language response

---

## Step 2: Create the Agent

Create a file named `ads_agent.py`:

```python
"""
Amazon Ads Agent — LangGraph + Anthropic API + Amazon Ads MCP Server

Connects to the Amazon Ads MCP Server via Streamable HTTP,
discovers tools, and orchestrates advertising workflows
using Claude via the Anthropic API.
"""

import os
import sys
import asyncio
import requests
from datetime import datetime
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TOOL_FILTER = os.getenv("TOOL_FILTER", "")


def log_info(msg):
    print(f"[INFO] {datetime.now().isoformat()} - {msg}", flush=True)


def log_error(msg):
    print(f"[ERROR] {datetime.now().isoformat()} - {msg}", file=sys.stderr, flush=True)


# ============================================================================
# OAuth Access Token
# ============================================================================

def get_access_token() -> str:
    """Exchange refresh token for a short-lived access token."""
    log_info("Requesting OAuth access token...")
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
        log_error(f"OAuth failed: {response.status_code} - {response.text[:200]}")
        sys.exit(1)
    log_info("Access token obtained")
    return response.json()["access_token"]


# ============================================================================
# Agent Builder
# ============================================================================

async def build_agent():
    """Connect to Ads MCP Server, discover tools, and create the agent."""

    # 1. Get access token
    access_token = get_access_token()

    # 2. Connect to Ads MCP Server
    log_info("Connecting to Amazon Ads MCP Server...")
    mcp_client = MultiServerMCPClient(
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

    # 3. Discover tools (with retry for rate limiting)
    import time
    for attempt in range(3):
        try:
            tools = await mcp_client.get_tools()
            log_info(f"Discovered {len(tools)} tools")
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = (attempt + 1) * 10
                log_info(f"Rate limited. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

    # 4. Filter tools (optional)
    allowed_groups = [g.strip() for g in TOOL_FILTER.split(",") if g.strip()]
    if allowed_groups:
        tools = [t for t in tools if any(t.name.startswith(g) for g in allowed_groups)]
        log_info(f"Filtered to {len(tools)} tools from groups: {allowed_groups}")

    for t in tools:
        log_info(f"  Tool: {t.name}")

    # 5. Initialize Claude via Anthropic API
    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=ANTHROPIC_API_KEY,
        temperature=0.1,
        max_tokens=2048,
    )

    # 6. Create the ReAct agent
    system_prompt = (
        "You are an Amazon Ads Campaign Assistant with access to the Amazon Ads API.\n\n"
        "## Your Capabilities\n"
        "- Query advertiser accounts and profiles\n"
        "- View, create, and manage campaigns\n"
        "- Request performance reports\n\n"
        "## Campaign Creation Workflow\n"
        "1. Call query_advertiser_accounts to identify available accounts.\n"
        "2. Ask the user which account/profile to use if not already known.\n"
        "3. Gather missing details: campaign name, budget, start date, targeting type.\n"
        "4. Confirm details with the user before creating.\n"
        "5. Call the appropriate campaign creation tool.\n"
        "6. Verify the campaign was created successfully.\n\n"
        "## Important Rules\n"
        "- Always confirm campaign details before executing creation or modification.\n"
        "- Default to Sponsored Products and manual targeting unless specified.\n"
        "- Budget must be at least $1.00. Start date must be YYYY-MM-DD format.\n"
    )

    agent = create_react_agent(llm, tools, prompt=system_prompt)
    log_info("Agent ready")
    return agent
```

---

## Step 3: Add the Interactive Loop

Add the interactive chat loop at the bottom of `ads_agent.py`:

```python
# ============================================================================
# Interactive Chat
# ============================================================================

async def chat(agent):
    """Run an interactive chat loop with the agent."""
    print("\n" + "=" * 60)
    print("Amazon Ads Agent — Interactive Mode")
    print("Type 'quit' to exit")
    print("=" * 60 + "\n")

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Goodbye!")
                break

            response = await agent.ainvoke(
                {"messages": [{"role": "user", "content": user_input}]}
            )
            reply = response["messages"][-1].content
            print(f"\nAgent: {reply}\n")

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            log_error(f"Error: {e}")
            print(f"\nError: {e}\n")


async def main():
    agent = await build_agent()
    await chat(agent)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 4: Run the Agent

```bash
python ads_agent.py
```

You should see the agent initialize, discover tools, and present an interactive prompt.

### Test Prompts

Try these prompts in order:

**1. Account Discovery:**
```
What advertiser accounts do I have access to?
```

**2. List Campaigns:**
```
List the campaigns on account <ACCOUNT_ID>
```
(Use an account ID from the previous response)

**3. Campaign Creation:**
```
Create a Sponsored Products campaign on account <ACCOUNT_ID> with name "Test Campaign", daily budget $10, state PAUSED
```

---

## Step 5: Review the Complete File Structure

```text
workshop-ads-agent/
├── .venv/
├── .env                  # Your credentials (do not commit)
├── requirements.txt      # Dependencies
├── verify_setup.py       # Environment check (Module 1)
├── test_connection.py    # MCP connection test (Module 2)
└── ads_agent.py          # The agent (this module)
```

**What you built:**

- OAuth token exchange to authenticate with the Amazon Ads MCP Server
- Tool discovery via Streamable HTTP with optional group filtering
- A LangGraph ReAct agent using Claude via the Anthropic API
- An interactive chat loop for testing advertising workflows

---

[← Previous: Connect to Amazon Ads MCP Server](02-connect-ads-mcp-server.md) | [Next: Testing and Cleanup →](04-testing-and-cleanup.md)
